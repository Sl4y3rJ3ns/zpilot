from dataclasses import dataclass, field, is_dataclass
from enum import Enum, StrEnum as _StrEnum, auto
from typing import get_origin

auto_obj = object()


def auto_field():
  return auto_obj


def apply_auto_fields(cls=None, /, **kwargs):
  cls_annotations = cls.__dict__.get('__annotations__', {})
  for name, typ in cls_annotations.items():
    current_value = getattr(cls, name, None)
    if current_value is auto_obj:
      origin_typ = get_origin(typ) or typ
      if isinstance(origin_typ, str):
        raise TypeError(f"Forward references are not supported for auto_field: '{origin_typ}'. Use a default_factory with lambda instead.")
      elif origin_typ in (int, float, str, bytes, list, tuple, set, dict, bool) or is_dataclass(origin_typ):
        setattr(cls, name, field(default_factory=origin_typ))
      elif origin_typ is None:
        setattr(cls, name, field(default=origin_typ))
      elif issubclass(origin_typ, Enum):  # first enum is the default
        setattr(cls, name, field(default=next(iter(origin_typ))))
      else:
        raise TypeError(f"Unsupported type for auto_field: {origin_typ}")
  return cls


class StrEnum(_StrEnum):
  @staticmethod
  def _generate_next_value_(name, *args):
    # auto() defaults to name.lower()
    return name


@dataclass
@apply_auto_fields
class RadarData:
  errors: list['Error'] = auto_field()
  points: list['RadarPoint'] = auto_field()

  class Error(StrEnum):
    canError = auto()
    fault = auto()
    wrongConfig = auto()

  @dataclass
  @apply_auto_fields
  class RadarPoint:
    trackId: int = auto_field()  # no trackId reuse

    # these 3 are the minimum required
    dRel: float = auto_field()  # m from the front bumper of the car
    yRel: float = auto_field()  # m
    vRel: float = auto_field()  # m/s

    # these are optional and valid if they are not NaN
    aRel: float = auto_field()  # m/s^2
    yvRel: float = auto_field()  # m/s

    # some radars flag measurements VS estimates
    measured: bool = auto_field()


@dataclass
@apply_auto_fields
class CarParams:
  carName: str = auto_field()
  carFingerprint: str = auto_field()
  fuzzyFingerprint: bool = auto_field()

  notCar: bool = auto_field()  # flag for non-car robotics platforms

  pcmCruise: bool = auto_field()  # is openpilot's state tied to the PCM's cruise state?
  enableDsu: bool = auto_field()  # driving support unit
  enableBsm: bool = auto_field()  # blind spot monitoring
  flags: int = auto_field()  # flags for car specific quirks
  experimentalLongitudinalAvailable: bool = auto_field()

  minEnableSpeed: float = auto_field()
  minSteerSpeed: float = auto_field()
  # safetyConfigs: list[SafetyConfig] = auto_field()
  alternativeExperience: int = auto_field()  # panda flag for features like no disengage on gas

  maxLateralAccel: float = auto_field()
  autoResumeSng: bool = auto_field()  # describes whether car can resume from a stop automatically

  mass: float = auto_field()  # [kg] curb weight: all fluids no cargo
  wheelbase: float = auto_field()  # [m] distance from rear axle to front axle
  centerToFront: float = auto_field()  # [m] distance from center of mass to front axle
  steerRatio: float = auto_field()  # [] ratio of steering wheel angle to front wheel angle
  steerRatioRear: float = auto_field()  # [] ratio of steering wheel angle to rear wheel angle (usually 0)

  rotationalInertia: float = auto_field()  # [kg*m2] body rotational inertia
  tireStiffnessFactor: float = auto_field()  # scaling factor used in calculating tireStiffness[Front,Rear]
  tireStiffnessFront: float = auto_field()  # [N/rad] front tire coeff of stiff
  tireStiffnessRear: float = auto_field()  # [N/rad] rear tire coeff of stiff

  longitudinalTuning: 'CarParams.LongitudinalPIDTuning' = field(default_factory=lambda: CarParams.LongitudinalPIDTuning())
  lateralParams: 'CarParams.LateralParams' = field(default_factory=lambda: CarParams.LateralParams())
  lateralTuning: 'CarParams.LateralTuning' = field(default_factory=lambda: CarParams.LateralTuning())

  @dataclass
  @apply_auto_fields
  class LateralTuning:
    def init(self, which: str):
      assert which in ('pid', 'torque'), 'Invalid union type'
      self.which = which

    which: str = 'pid'

    pid: 'CarParams.LateralPIDTuning' = field(default_factory=lambda: CarParams.LateralPIDTuning())
    torque: 'CarParams.LateralTorqueTuning' = field(default_factory=lambda: CarParams.LateralTorqueTuning())

  @dataclass
  @apply_auto_fields
  class LateralParams:
    torqueBP: list[int] = auto_field()
    torqueV: list[int] = auto_field()

  @dataclass
  @apply_auto_fields
  class LateralPIDTuning:
    kpBP: list[float] = auto_field()
    kpV: list[float] = auto_field()
    kiBP: list[float] = auto_field()
    kiV: list[float] = auto_field()
    kf: float = auto_field()

  @dataclass
  @apply_auto_fields
  class LateralTorqueTuning:
    useSteeringAngle: bool = auto_field()
    kp: float = auto_field()
    ki: float = auto_field()
    friction: float = auto_field()
    kf: float = auto_field()
    steeringAngleDeadzoneDeg: float = auto_field()
    latAccelFactor: float = auto_field()
    latAccelOffset: float = auto_field()

  steerLimitAlert: bool = auto_field()
  steerLimitTimer: float = auto_field()  # time before steerLimitAlert is issued

  vEgoStopping: float = auto_field()  # Speed at which the car goes into stopping state
  vEgoStarting: float = auto_field()  # Speed at which the car goes into starting state
  stoppingControl: bool = auto_field()  # Does the car allow full control even at lows speeds when stopping
  steerControlType: 'CarParams.SteerControlType' = field(default_factory=lambda: CarParams.SteerControlType.torque)
  radarUnavailable: bool = auto_field()  # True when radar objects aren't visible on CAN or aren't parsed out
  stopAccel: float = auto_field()  # Required acceleration to keep vehicle stationary
  stoppingDecelRate: float = auto_field()  # m/s^2/s while trying to stop
  startAccel: float = auto_field()  # Required acceleration to get car moving
  startingState: bool = auto_field()  # Does this car make use of special starting state

  steerActuatorDelay: float = auto_field()  # Steering wheel actuator delay in seconds
  longitudinalActuatorDelay: float = auto_field()  # Gas/Brake actuator delay in seconds
  openpilotLongitudinalControl: bool = auto_field()  # is openpilot doing the longitudinal control?
  carVin: str = auto_field()  # VIN number queried during fingerprinting
  dashcamOnly: bool = auto_field()
  passive: bool = auto_field()  # is openpilot in control?
  # transmissionType: TransmissionType = auto_field()
  carFw: list['CarParams.CarFw'] = auto_field()

  radarTimeStep: float = 0.05  # time delta between radar updates, 20Hz is very standard
  # fingerprintSource: FingerprintSource = auto_field()
  # networkLocation: NetworkLocation = auto_field()  # Where Panda/C2 is integrated into the car's CAN network

  wheelSpeedFactor: float = auto_field()  # Multiplier on wheels speeds to computer actual speeds

  @dataclass
  @apply_auto_fields
  class LongitudinalPIDTuning:
    kpBP: list[float] = auto_field()
    kpV: list[float] = auto_field()
    kiBP: list[float] = auto_field()
    kiV: list[float] = auto_field()
    kf: float = auto_field()

  class SteerControlType(StrEnum):
    torque = auto()
    angle = auto()

  @dataclass
  @apply_auto_fields
  class CarFw:
    ecu: 'CarParams.Ecu' = field(default_factory=lambda: CarParams.Ecu.unknown)
    fwVersion: bytes = auto_field()
    address: int = auto_field()
    subAddress: int = auto_field()
    responseAddress: int = auto_field()
    request: list[bytes] = auto_field()
    brand: str = auto_field()
    bus: int = auto_field()
    logging: bool = auto_field()
    obdMultiplexing: bool = auto_field()

  class Ecu(StrEnum):
    eps = auto()
    abs = auto()
    fwdRadar = auto()
    fwdCamera = auto()
    engine = auto()
    unknown = auto()
    transmission = auto()  # Transmission Control Module
    hybrid = auto()  # hybrid control unit, e.g. Chrysler's HCP, Honda's IMA Control Unit, Toyota's hybrid control computer
    srs = auto()  # airbag
    gateway = auto()  # can gateway
    hud = auto()  # heads up display
    combinationMeter = auto()  # instrument cluster
    electricBrakeBooster = auto()
    shiftByWire = auto()
    adas = auto()
    cornerRadar = auto()
    hvac = auto()
    parkingAdas = auto()  # parking assist system ECU, e.g. Toyota's IPAS, Hyundai's RSPA, etc.
    epb = auto()  # electronic parking brake
    telematics = auto()
    body = auto()  # body control module

    # Toyota only
    dsu = auto()

    # Honda only
    vsa = auto()  # Vehicle Stability Assist
    programmedFuelInjection = auto()

    debug = auto()
