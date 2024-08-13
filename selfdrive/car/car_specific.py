import json
import os
import numpy as np
import tomllib
from abc import abstractmethod, ABC
from enum import StrEnum
from typing import Any, NamedTuple
from collections.abc import Callable
from functools import cache
from dataclasses import dataclass

from cereal import car
from openpilot.common.basedir import BASEDIR
from openpilot.common.simple_kalman import KF1D, get_kalman_gain
from openpilot.selfdrive.car import DT_CTRL, apply_hysteresis, gen_empty_fingerprint, scale_rot_inertia, scale_tire_stiffness, get_friction, STD_CARGO_KG
from openpilot.selfdrive.car.interfaces import MAX_CTRL_SPEED
from openpilot.selfdrive.car.volkswagen.values import CarControllerParams as VWCarControllerParams
from openpilot.selfdrive.car.hyundai.interface import ENABLE_BUTTONS as HYUNDAI_ENABLE_BUTTONS
from openpilot.selfdrive.car.can_definitions import CanData, CanRecvCallable, CanSendCallable
from openpilot.selfdrive.car.conversions import Conversions as CV
from openpilot.selfdrive.car.helpers import clip
from openpilot.selfdrive.car.values import PLATFORMS
from openpilot.selfdrive.controls.lib.events import Events

ButtonType = car.CarState.ButtonEvent.Type
GearShifter = car.CarState.GearShifter
EventName = car.CarEvent.EventName
NetworkLocation = car.CarParams.NetworkLocation


class CarSpecificEvents:
  def __init__(self, CP: car.CarParams):
    self.CP = CP

    self.steering_unpressed = 0
    self.low_speed_alert = False
    self.no_steer_warning = False
    self.silent_steer_warning = True

  def update(self, CS, CS_prev, CC_prev):
    if self.CP.carName in ('tesla', 'subaru'):
      events = self.create_common_events(CS, CS_prev)

    elif self.CP.carName == 'ford':
      events = self.create_common_events(CS, CS_prev, extra_gears=[GearShifter.manumatic])

    elif self.CP.carName == 'nissan':
      events = self.create_common_events(CS, CS_prev, extra_gears=[car.CarState.GearShifter.brake])

      if CS.lkas_enabled:
        events.add(car.CarEvent.EventName.invalidLkasSetting)

    elif self.CP.carName == 'mazda':
      events = self.create_common_events(CS, CS_prev)

      if CS.lkas_disabled:
        events.add(EventName.lkasDisabled)
      elif CS.low_speed_alert:
        events.add(EventName.belowSteerSpeed)

    elif self.CP.carName == 'chrysler':
      events = self.create_common_events(CS, CS_prev, extra_gears=[car.CarState.GearShifter.low])

      # Low speed steer alert hysteresis logic
      if self.CP.minSteerSpeed > 0. and CS.out.vEgo < (self.CP.minSteerSpeed + 0.5):
        self.low_speed_alert = True
      elif CS.out.vEgo > (self.CP.minSteerSpeed + 1.):
        self.low_speed_alert = False
      if self.low_speed_alert:
        events.add(car.CarEvent.EventName.belowSteerSpeed)

    elif self.CP.carName == 'honda':
      events = self.create_common_events(CS, CS_prev, pcm_enable=False)

      if self.CP.pcmCruise and CS.out.vEgo < self.CP.minEnableSpeed:
        events.add(EventName.belowEngageSpeed)

      if self.CP.pcmCruise:
        # we engage when pcm is active (rising edge)
        if CS.out.cruiseState.enabled and not CS_prev.cruiseState.enabled:
          events.add(EventName.pcmEnable)
        elif not CS.out.cruiseState.enabled and (CC_prev.actuators.accel >= 0. or not self.CP.openpilotLongitudinalControl):
          # it can happen that car cruise disables while comma system is enabled: need to
          # keep braking if needed or if the speed is very low
          if CS.out.vEgo < self.CP.minEnableSpeed + 2.:
            # non loud alert if cruise disables below 25mph as expected (+ a little margin)
            events.add(EventName.speedTooLow)
          else:
            events.add(EventName.cruiseDisabled)
      if self.CP.minEnableSpeed > 0 and CS.out.vEgo < 0.001:
        events.add(EventName.manualRestart)

    elif self.CP.carName == 'toyota':
      events = self.create_common_events(CS, CS_prev)

      if self.CP.openpilotLongitudinalControl:
        if CS.out.cruiseState.standstill and not CS.out.brakePressed:
          events.add(EventName.resumeRequired)
        if CS.low_speed_lockout:
          events.add(EventName.lowSpeedLockout)
        if CS.out.vEgo < self.CP.minEnableSpeed:
          events.add(EventName.belowEngageSpeed)
          if CC_prev.actuators.accel > 0.3:
            # some margin on the actuator to not false trigger cancellation while stopping
            events.add(EventName.speedTooLow)
          if CS.out.vEgo < 0.001:
            # while in standstill, send a user alert
            events.add(EventName.manualRestart)

    elif self.CP.carName == 'gm':
      # The ECM allows enabling on falling edge of set, but only rising edge of resume
      events = self.create_common_events(CS, CS_prev, extra_gears=[GearShifter.sport, GearShifter.low,
                                                                   GearShifter.eco, GearShifter.manumatic],
                                         pcm_enable=self.CP.pcmCruise, enable_buttons=(ButtonType.decelCruise,))
      if not self.CP.pcmCruise:
        if any(b.type == ButtonType.accelCruise and b.pressed for b in CS.out.buttonEvents):
          events.add(EventName.buttonEnable)

      # Enabling at a standstill with brake is allowed
      # TODO: verify 17 Volt can enable for the first time at a stop and allow for all GMs
      below_min_enable_speed = CS.out.vEgo < self.CP.minEnableSpeed or CS.moving_backward
      if below_min_enable_speed and not (CS.out.standstill and CS.out.brake >= 20 and
                                         self.CP.networkLocation == NetworkLocation.fwdCamera):
        events.add(EventName.belowEngageSpeed)
      if CS.out.cruiseState.standstill:
        events.add(EventName.resumeRequired)
      if CS.out.vEgo < self.CP.minSteerSpeed:
        events.add(EventName.belowSteerSpeed)

    elif self.CP.carName == 'volkswagen':
      events = self.create_common_events(CS, CS_prev, extra_gears=[GearShifter.eco, GearShifter.sport, GearShifter.manumatic],
                                         pcm_enable=not self.CP.openpilotLongitudinalControl,
                                         enable_buttons=(ButtonType.setCruise, ButtonType.resumeCruise))

      # Low speed steer alert hysteresis logic
      if (self.CP.minSteerSpeed - 1e-3) > VWCarControllerParams.DEFAULT_MIN_STEER_SPEED and CS.out.vEgo < (self.CP.minSteerSpeed + 1.):
        self.low_speed_alert = True
      elif CS.out.vEgo > (self.CP.minSteerSpeed + 2.):
        self.low_speed_alert = False
      if self.low_speed_alert:
        events.add(EventName.belowSteerSpeed)

      if self.CP.openpilotLongitudinalControl:
        if CS.out.vEgo < self.CP.minEnableSpeed + 0.5:
          events.add(EventName.belowEngageSpeed)
        if CC_prev.enabled and CS.out.vEgo < self.CP.minEnableSpeed:
          events.add(EventName.speedTooLow)

      # TODO: we don't have the CC here
      # if self.CC.eps_timer_soft_disable_alert:
      #   events.add(EventName.steerTimeLimit)

    elif self.CP.carName == 'hyundai':
      # On some newer model years, the CANCEL button acts as a pause/resume button based on the PCM state
      # To avoid re-engaging when openpilot cancels, check user engagement intention via buttons
      # Main button also can trigger an engagement on these cars
      allow_enable = any(btn in HYUNDAI_ENABLE_BUTTONS for btn in CS.cruise_buttons) or any(CS.main_buttons)
      events = self.create_common_events(CS, CS_prev, pcm_enable=self.CP.pcmCruise, allow_enable=allow_enable)

      # low speed steer alert hysteresis logic (only for cars with steer cut off above 10 m/s)
      if CS.out.vEgo < (self.CP.minSteerSpeed + 2.) and self.CP.minSteerSpeed > 10.:
        self.low_speed_alert = True
      if CS.out.vEgo > (self.CP.minSteerSpeed + 4.):
        self.low_speed_alert = False
      if self.low_speed_alert:
        events.add(car.CarEvent.EventName.belowSteerSpeed)

    elif self.CP.carName == 'body':
      events = Events()

    else:
      raise ValueError(f"Unsupported car: {self.CP.carName}")

    return events

  def create_common_events(self, CS, CS_prev, extra_gears=None, pcm_enable=True, allow_enable=True,
                           enable_buttons=(ButtonType.accelCruise, ButtonType.decelCruise)):
    events = Events()

    if CS.out.doorOpen:
      events.add(EventName.doorOpen)
    if CS.out.seatbeltUnlatched:
      events.add(EventName.seatbeltNotLatched)
    if CS.out.gearShifter != GearShifter.drive and (extra_gears is None or
       CS.out.gearShifter not in extra_gears):
      events.add(EventName.wrongGear)
    if CS.out.gearShifter == GearShifter.reverse:
      events.add(EventName.reverseGear)
    if not CS.out.cruiseState.available:
      events.add(EventName.wrongCarMode)
    if CS.out.espDisabled:
      events.add(EventName.espDisabled)
    if CS.out.espActive:
      events.add(EventName.espActive)
    if CS.out.stockFcw:
      events.add(EventName.stockFcw)
    if CS.out.stockAeb:
      events.add(EventName.stockAeb)
    if CS.out.vEgo > MAX_CTRL_SPEED:
      events.add(EventName.speedTooHigh)
    if CS.out.cruiseState.nonAdaptive:
      events.add(EventName.wrongCruiseMode)
    if CS.out.brakeHoldActive and self.CP.openpilotLongitudinalControl:
      events.add(EventName.brakeHold)
    if CS.out.parkingBrake:
      events.add(EventName.parkBrake)
    if CS.out.accFaulted:
      events.add(EventName.accFaulted)
    if CS.out.steeringPressed:
      events.add(EventName.steerOverride)
    if CS.out.brakePressed and CS.out.standstill:
      events.add(EventName.preEnableStandstill)
    if CS.out.gasPressed:
      events.add(EventName.gasPressedOverride)
    if CS.out.vehicleSensorsInvalid:
      events.add(EventName.vehicleSensorsInvalid)

    # Handle button presses
    for b in CS.out.buttonEvents:
      # Enable OP long on falling edge of enable buttons (defaults to accelCruise and decelCruise, overridable per-port)
      if not self.CP.pcmCruise and (b.type in enable_buttons and not b.pressed):
        events.add(EventName.buttonEnable)
      # Disable on rising and falling edge of cancel for both stock and OP long
      if b.type == ButtonType.cancel:
        events.add(EventName.buttonCancel)

    # Handle permanent and temporary steering faults
    self.steering_unpressed = 0 if CS.out.steeringPressed else self.steering_unpressed + 1
    if CS.out.steerFaultTemporary:
      if CS.out.steeringPressed and (not CS_prev.steerFaultTemporary or self.no_steer_warning):
        self.no_steer_warning = True
      else:
        self.no_steer_warning = False

        # if the user overrode recently, show a less harsh alert
        if self.silent_steer_warning or CS.out.standstill or self.steering_unpressed < int(1.5 / DT_CTRL):
          self.silent_steer_warning = True
          events.add(EventName.steerTempUnavailableSilent)
        else:
          events.add(EventName.steerTempUnavailable)
    else:
      self.no_steer_warning = False
      self.silent_steer_warning = False
    if CS.out.steerFaultPermanent:
      events.add(EventName.steerUnavailable)

    # we engage when pcm is active (rising edge)
    # enabling can optionally be blocked by the car interface
    if pcm_enable:
      if CS.out.cruiseState.enabled and not CS_prev.cruiseState.enabled and allow_enable:
        events.add(EventName.pcmEnable)
      elif not CS.out.cruiseState.enabled:
        events.add(EventName.pcmDisable)

    return events
