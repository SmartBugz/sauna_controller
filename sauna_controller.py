"""Background logic for the smart sauna controller.

Responsibilities:
- Read temperature from DS18B20 (with mock fallback for PC)
- Control a GPIO relay driving the sauna heater (active-LOW)
- Maintain shared state (current temp, desired temp, on/off status, timings)
- Persist configuration/state (desired setpoint and heater enabled flag) to JSON
- Run a background control loop with simple bang-bang hysteresis control

This module is written so it can run both on a Raspberry Pi 4 and on a
regular PC for development:
- On Pi: uses RPi.GPIO and 1-Wire device files under /sys/bus/w1/devices
- On PC: falls back to mock GPIO and a fake temperature that slowly changes
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Optional

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "sauna_state.json")

# GPIO pin numbers (BCM mode)
TEMP_SENSOR_GPIO = 4  # DS18B20 uses 1-Wire on GPIO4 (handled by kernel)
RELAY_GPIO = 17       # Relay control pin (active-LOW)

HYSTERESIS = 1.0  # degrees C
CONTROL_INTERVAL_SEC = 2.0  # control loop period


# --- GPIO Abstraction -------------------------------------------------------

class BaseGPIO:
    """Minimal GPIO abstraction so we can swap real vs mock implementation."""

    BCM = "BCM"
    OUT = "OUT"

    def setmode(self, mode):  # pragma: no cover - simple pass-through
        pass

    def setup(self, pin, mode):  # pragma: no cover - simple pass-through
        pass

    def output(self, pin, value):  # pragma: no cover - simple pass-through
        pass

    def cleanup(self):  # pragma: no cover - simple pass-through
        pass


class MockGPIO(BaseGPIO):
    """In-memory mock of GPIO for development on non-Pi machines."""

    def __init__(self):
        self.state = {}

    def setmode(self, mode):
        # No-op for mock
        self.mode = mode

    def setup(self, pin, mode):
        self.state[pin] = True  # default relay off (inactive, HIGH)

    def output(self, pin, value):
        self.state[pin] = value

    def cleanup(self):
        self.state.clear()


try:  # Try to import real GPIO; fall back to mock on error
    import RPi.GPIO as RPiGPIO  # type: ignore

    class RealGPIO(BaseGPIO):
        def __init__(self):
            self.gpio = RPiGPIO

        def setmode(self, mode):
            if mode == self.BCM:
                self.gpio.setmode(self.gpio.BCM)

        def setup(self, pin, mode):
            if mode == self.OUT:
                self.gpio.setup(pin, self.gpio.OUT)

        def output(self, pin, value):
            self.gpio.output(pin, value)

        def cleanup(self):
            self.gpio.cleanup()

    GPIO: BaseGPIO = RealGPIO()
except Exception:  # pragma: no cover - executed only off-Pi
    GPIO = MockGPIO()


# --- Temperature Reading ----------------------------------------------------


def _find_w1_device_path() -> Optional[str]:
    """Locate the DS18B20 w1_slave file if running on a Pi.

    Returns the full path to the w1_slave file, or None if not found.
    """
    base_dir = "/sys/bus/w1/devices"
    if not os.path.isdir(base_dir):
        return None

    for name in os.listdir(base_dir):
        if name.startswith("28-"):
            device_file = os.path.join(base_dir, name, "w1_slave")
            if os.path.isfile(device_file):
                return device_file
    return None


W1_DEVICE_FILE = _find_w1_device_path()


def read_temp() -> float:
    """Read temperature from DS18B20 if available, otherwise return mock value.

    On Raspberry Pi with 1-Wire enabled, reads from /sys/bus/w1/devices/28-*/w1_slave.
    On a development PC, returns a synthetic temperature that slowly oscillates.
    """
    if W1_DEVICE_FILE and os.path.isfile(W1_DEVICE_FILE):
        # Real sensor mode
        try:
            with open(W1_DEVICE_FILE, "r") as f:
                lines = f.readlines()
            # Example lines:
            #  f3 01 4b 46 7f ff 0c 10 5e : crc=5e YES
            #  f3 01 4b 46 7f ff 0c 10 5e t=31062
            if len(lines) >= 2 and "YES" in lines[0] and "t=" in lines[1]:
                temp_str = lines[1].split("t=")[-1].strip()
                temp_c = float(temp_str) / 1000.0
                return temp_c
        except Exception:
            # Fall through to mock on any failure
            pass

    # Mock mode: simple synthetic wave between 20 and 80 degrees
    t = time.time()
    base = 50.0
    amplitude = 30.0
    # Use a slow sine-like pattern via cosine
    import math

    return base + amplitude * math.sin(t / 120.0)


# --- Shared State -----------------------------------------------------------


@dataclass
class SaunaState:
    current_temp: float = 0.0
    desired_temp: float = 70.0  # Default desired temperature in C
    heater_enabled: bool = False  # User has explicitly allowed heater operation
    heater_on: bool = False  # Actual relay state (True means heater energized)
    heater_on_since: Optional[float] = None  # epoch seconds
    time_to_setpoint: Optional[float] = None  # seconds from heater_on_since

    last_updated: Optional[float] = None  # epoch seconds


class SaunaController:
    """Manages background reading and heater control in its own thread."""

    def __init__(self, config_path: str = CONFIG_PATH):
        self.config_path = config_path
        self._lock = threading.Lock()
        self._state = SaunaState()
        self._load_state_from_disk()

        # Initialize GPIO
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(RELAY_GPIO, GPIO.OUT)

        # Ensure relay is off initially (active-LOW -> set HIGH)
        GPIO.output(RELAY_GPIO, True)

        # Start background control thread (daemon so it won't block process exit)
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    # --- Public API used by web app ----------------------------------------

    def get_state_snapshot(self) -> dict:
        """Return a thread-safe snapshot of the current state as a dict.

        Suitable for passing directly to the template.
        """
        with self._lock:
            state = asdict(self._state)

        # Convert timestamps to friendly durations (seconds -> HH:MM:SS)
        now = time.time()

        heater_on_duration = None
        if state["heater_on"] and state["heater_on_since"]:
            heater_on_duration = now - state["heater_on_since"]

        time_to_setpoint = state["time_to_setpoint"]

        def _fmt_duration(seconds: Optional[float]) -> Optional[str]:
            if seconds is None:
                return None
            seconds_int = int(seconds)
            h, rem = divmod(seconds_int, 3600)
            m, s = divmod(rem, 60)
            return f"{h:02d}:{m:02d}:{s:02d}"

        return {
            "current_temp": round(state["current_temp"], 1),
            "desired_temp": state["desired_temp"],
            "heater_enabled": state["heater_enabled"],
            "heater_on": state["heater_on"],
            "heater_on_for": _fmt_duration(heater_on_duration),
            "time_to_setpoint": _fmt_duration(time_to_setpoint),
        }

    def set_heater_enabled(self, enabled: bool) -> None:
        """User-facing toggle: when False, heater will be forced off."""
        with self._lock:
            self._state.heater_enabled = enabled
            if not enabled:
                # Immediately turn relay off
                self._set_relay(False)
            self._save_state_to_disk_locked()

    def set_desired_temperature(self, temp: float) -> None:
        """Update desired temperature and persist configuration."""
        with self._lock:
            self._state.desired_temp = temp
            # Reset setpoint timing if user changes the target
            self._state.time_to_setpoint = None
            self._save_state_to_disk_locked()

    def stop(self) -> None:
        """Signal the background loop to stop and cleanup GPIO."""
        self._stop_event.set()
        self._thread.join(timeout=2.0)
        GPIO.cleanup()

    # --- Internal helpers ---------------------------------------------------

    def _set_relay(self, on: bool) -> None:
        """Drive the relay, honoring active-LOW behavior.

        on=True  -> GPIO LOW  -> heater ON
        on=False -> GPIO HIGH -> heater OFF
        """
        self._state.heater_on = on
        if on:
            GPIO.output(RELAY_GPIO, False)  # active-LOW
            if self._state.heater_on_since is None:
                self._state.heater_on_since = time.time()
        else:
            GPIO.output(RELAY_GPIO, True)
            self._state.heater_on_since = None

    def _load_state_from_disk(self) -> None:
        """Load persisted configuration if present (desired_temp, heater_enabled)."""
        if not os.path.isfile(self.config_path):
            return
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return

        with self._lock:
            self._state.desired_temp = float(data.get("desired_temp", self._state.desired_temp))
            self._state.heater_enabled = bool(data.get("heater_enabled", self._state.heater_enabled))

    def _save_state_to_disk_locked(self) -> None:
        """Persist relevant configuration fields to JSON.

        Caller must hold self._lock.
        """
        data = {
            "desired_temp": self._state.desired_temp,
            "heater_enabled": self._state.heater_enabled,
        }
        try:
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception:
            # Ignore persistence errors in control loop; log in real system
            pass

    def _run_loop(self) -> None:
        """Background control loop.

        Runs until stop() is called. Periodically reads temperature and
        applies simple bang-bang control with hysteresis.
        """
        while not self._stop_event.is_set():
            now = time.time()
            current_temp = read_temp()

            with self._lock:
                self._state.current_temp = current_temp
                self._state.last_updated = now

                desired = self._state.desired_temp
                enabled = self._state.heater_enabled

                if not enabled:
                    # User has disabled heater entirely
                    self._set_relay(False)
                else:
                    # Bang-bang control with hysteresis
                    if current_temp < desired - HYSTERESIS:
                        self._set_relay(True)
                    elif current_temp > desired + HYSTERESIS:
                        self._set_relay(False)

                # Track time to first reach setpoint
                if (
                    self._state.heater_on_since is not None
                    and self._state.time_to_setpoint is None
                    and current_temp >= desired
                ):
                    self._state.time_to_setpoint = now - self._state.heater_on_since

            time.sleep(CONTROL_INTERVAL_SEC)


if __name__ == "__main__":
    # Simple manual test harness: print state periodically
    controller = SaunaController()
    try:
        while True:
            print(controller.get_state_snapshot())
            time.sleep(5)
    except KeyboardInterrupt:
        controller.stop()
