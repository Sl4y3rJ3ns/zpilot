#include <sys/resource.h>

#include <chrono>
#include <thread>
#include <vector>
#include <map>
#include <poll.h>
#include <linux/gpio.h>

#include "cereal/services.h"
#include "cereal/messaging/messaging.h"
#include "common/i2c.h"
#include "common/ratekeeper.h"
#include "common/swaglog.h"
#include "common/timing.h"
#include "common/util.h"
#include "system/sensord/sensors/bmx055_accel.h"
#include "system/sensord/sensors/bmx055_gyro.h"
#include "system/sensord/sensors/bmx055_magn.h"
#include "system/sensord/sensors/bmx055_temp.h"
#include "system/sensord/sensors/constants.h"
#include "system/sensord/sensors/lsm6ds3_accel.h"
#include "system/sensord/sensors/lsm6ds3_gyro.h"
#include "system/sensord/sensors/lsm6ds3_temp.h"
#include "system/sensord/sensors/mmc5603nj_magn.h"

#define I2C_BUS_IMU 1

ExitHandler do_exit;
const int MAX_EVENTS = 32;

// Read all GPIO events and return the latest event timestamp
uint64_t get_latest_event_time(int fd) {
  uint64_t latest_ts = 0;
  const uint64_t offset = nanos_since_epoch() - nanos_since_boot();

  while (true) {
    struct gpioevent_data evdata[MAX_EVENTS];
    int bytes_read = HANDLE_EINTR(read(fd, evdata, sizeof(evdata)));

    if (bytes_read < 0) {
      LOGE("error reading event data: %s", strerror(errno));
      break;  // Exit loop on read error
    }

    if (bytes_read == 0) {
      break;  // no more events
    }

    if (bytes_read % sizeof(*evdata) != 0) {
      LOGE("Unexpected number of bytes read: %d", bytes_read);
      break;  // Exit loop on data inconsistency
    }

    int num_events = bytes_read / sizeof(struct gpioevent_data);
    for (int i = 0; i < num_events; ++i) {
      latest_ts = std::max<uint64_t>(latest_ts, evdata[i].timestamp - offset);
    }
  }
  return latest_ts;
}

void interrupt_loop(std::vector<std::tuple<Sensor *, std::string>> sensors) {
  PubMaster pm({"gyroscope", "accelerometer"});

  int fd = -1;
  for (auto &[sensor, msg_name] : sensors) {
    if (sensor->has_interrupt_enabled()) {
      fd = sensor->gpio_fd;
      break;
    }
  }

  struct pollfd fd_list[1] = {0};
  fd_list[0].fd = fd;
  fd_list[0].events = POLLIN | POLLPRI;

  while (!do_exit) {
    int err = poll(fd_list, 1, 100);
    if (err == -1) {
      if (errno == EINTR) {
        continue;
      }
      return;
    } else if (err == 0) {
      LOGE("poll timed out");
      continue;
    }

    if ((fd_list[0].revents & (POLLIN | POLLPRI)) == 0) {
      LOGE("no poll events set");
      continue;
    }

    uint64_t ts = get_latest_event_time(fd);
    if (ts == 0) {
      continue;  // No valid timestamp, continue to the next iteration
    }

    for (auto &[sensor, msg_name] : sensors) {
      if (!sensor->has_interrupt_enabled()) {
        continue;
      }

      MessageBuilder msg;
      if (!sensor->get_event(msg, ts)) {
        continue;
      }

      if (!sensor->is_data_valid(ts)) {
        continue;
      }

      pm.send(msg_name.c_str(), msg);
    }
  }
}

void polling_loop(Sensor *sensor, std::string msg_name) {
  PubMaster pm({msg_name.c_str()});
  RateKeeper rk(msg_name, services.at(msg_name).frequency);
  while (!do_exit) {
    MessageBuilder msg;
    if (sensor->get_event(msg) && sensor->is_data_valid(nanos_since_boot())) {
      pm.send(msg_name.c_str(), msg);
    }
    rk.keepTime();
  }
}

int sensor_loop(I2CBus *i2c_bus_imu) {
  // Sensor init
  std::vector<std::tuple<Sensor *, std::string>> sensors_init = {
    {new BMX055_Accel(i2c_bus_imu), "accelerometer2"},
    {new BMX055_Gyro(i2c_bus_imu), "gyroscope2"},
    {new BMX055_Magn(i2c_bus_imu), "magnetometer"},
    {new BMX055_Temp(i2c_bus_imu), "temperatureSensor2"},

    {new LSM6DS3_Accel(i2c_bus_imu, GPIO_LSM_INT), "accelerometer"},
    {new LSM6DS3_Gyro(i2c_bus_imu, GPIO_LSM_INT, true), "gyroscope"},
    {new LSM6DS3_Temp(i2c_bus_imu), "temperatureSensor"},

    {new MMC5603NJ_Magn(i2c_bus_imu), "magnetometer"},
  };

  // Initialize sensors
  std::vector<std::thread> threads;
  for (auto &[sensor, msg_name] : sensors_init) {
    int err = sensor->init();
    if (err < 0) {
      continue;
    }

    if (!sensor->has_interrupt_enabled()) {
      threads.emplace_back(polling_loop, sensor, msg_name);
    }
  }

  // increase interrupt quality by pinning interrupt and process to core 1
  setpriority(PRIO_PROCESS, 0, -18);
  util::set_core_affinity({1});

  // TODO: get the IRQ number from gpiochip
  std::string irq_path = "/proc/irq/336/smp_affinity_list";
  if (!util::file_exists(irq_path)) {
    irq_path = "/proc/irq/335/smp_affinity_list";
  }
  std::system(util::string_format("sudo su -c 'echo 1 > %s'", irq_path.c_str()).c_str());

  // thread for reading events via interrupts
  threads.emplace_back(&interrupt_loop, std::ref(sensors_init));

  // wait for all threads to finish
  for (auto &t : threads) {
    t.join();
  }

  for (auto &[sensor, msg_name] : sensors_init) {
    sensor->shutdown();
    delete sensor;
  }
  return 0;
}

int main(int argc, char *argv[]) {
  try {
    auto i2c_bus_imu = std::make_unique<I2CBus>(I2C_BUS_IMU);
    return sensor_loop(i2c_bus_imu.get());
  } catch (std::exception &e) {
    LOGE("I2CBus init failed");
    return -1;
  }
}
