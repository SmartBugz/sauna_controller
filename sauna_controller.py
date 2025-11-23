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
from typing import Optional

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "sauna_state.json")

# GPIO pin numbers (BCM mode)
TEMP_SENSOR_GPIO = 4  # DS18B20 uses 1-Wire on GPIO4 (handled by kernel)
RELAY_GPIO = 17       # Relay control pin (active-LOW)

HYSTERESIS = 2.5  # degrees C
MAX_TEMP_C = 90.0  # safety limit (~194°F), below 100°C high-limit switch
MAX_ON_TIME_SEC = 2 * 60 * 60  # 2 hours continuous ON time
CONFIRMATION_TIMEOUT_SEC = 90  # time window to confirm extending runtime
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


def _find_all_w1_devices() -> list[str]:
    """Locate all DS18B20 w1_slave files if running on a Pi.

    Returns a list of full paths to w1_slave files, or empty list if none found.
    """
    base_dir = "/sys/bus/w1/devices"
    if not os.path.isdir(base_dir):
        return []

    devices = []
    for name in os.listdir(base_dir):
        if name.startswith("28-"):
            device_file = os.path.join(base_dir, name, "w1_slave")
            if os.path.isfile(device_file):
                devices.append(device_file)
    return devices


W1_DEVICE_FILES = _find_all_w1_devices()


def _read_sensor(device_path: str) -> Optional[float]:
    """Read temperature from a specific DS18B20 device file.

    Returns temp in Celsius or None on error.
    """
    try:
        with open(device_path, "r") as f:
            lines = f.readlines()
        # Example lines:
        #  f3 01 4b 46 7f ff 0c 10 5e : crc=5e YES
        #  f3 01 4b 46 7f ff 0c 10 5e t=31062
        if len(lines) >= 2 and "YES" in lines[0] and "t=" in lines[1]:
            temp_str = lines[1].split("t=")[-1].strip()
            temp_c = float(temp_str) / 1000.0
            return temp_c
    except Exception:
        pass
    return None


def read_temps() -> tuple[float, Optional[float]]:
    """Read temperatures from DS18B20 sensors if available.

    Returns (bench_temp, ceiling_temp) in Celsius.
    - bench_temp: primary sensor for user control (first sensor or mock).
    - ceiling_temp: safety sensor near ceiling (second sensor or None).

    On a development PC, returns synthetic values.
    """
    if W1_DEVICE_FILES:
        # Real sensor mode
        bench_temp = _read_sensor(W1_DEVICE_FILES[0])
        ceiling_temp = _read_sensor(W1_DEVICE_FILES[1]) if len(W1_DEVICE_FILES) > 1 else None

        if bench_temp is not None:
            return (bench_temp, ceiling_temp)

    # Mock mode: simple synthetic wave between 20 and 80 degrees for bench,
    # ceiling slightly hotter
    t = time.time()
    base = 50.0
    amplitude = 30.0
    import math

    bench_mock = base + amplitude * math.sin(t / 120.0)
    ceiling_mock = bench_mock + 5.0  # ceiling always ~5°C hotter in mock
    return (bench_mock, ceiling_mock)


# --- Shared State -----------------------------------------------------------


@dataclass
class SaunaState:
    current_temp: float = 0.0  # Bench-level temp (primary)
    ceiling_temp: Optional[float] = None  # Ceiling-level temp (safety)
    desired_temp: float = 70.0  # Default desired temperature in C
    heater_enabled: bool = False  # User has explicitly allowed heater operation
    heater_on: bool = False  # Actual relay state (True means heater energized)
    heater_on_since: Optional[float] = None  # epoch seconds
    time_to_setpoint: Optional[float] = None  # seconds from heater_on_since

    last_updated: Optional[float] = None  # epoch seconds
    use_imperial: bool = True  # Display temperatures in Fahrenheit by default
    lockout_active: bool = False  # True when max-on-time lockout is in effect
    lockout_reason: Optional[str] = None  # e.g. "max_on_time"
    confirmation_required: bool = False  # waiting for user to confirm continuation
    confirmation_deadline: Optional[float] = None  # epoch seconds

    # Simple one-shot schedule: UTC timestamp (epoch seconds) when we want sauna hot
    scheduled_start_at: Optional[float] = None
    # Measured average heat-up rate in deg C per second (for scheduling)
    avg_heatup_rate_c_per_sec: Optional[float] = None

    # Simple energy/cost tracking configuration
    price_per_kwh: Optional[float] = None  # in user currency
    heater_power_kw: Optional[float] = None  # e.g. 6.0 for 6 kW heater

    # Simple timer/stopwatch state (UI only for now)
    timer_mode: str = "stopwatch"  # "stopwatch" or "timer"
    timer_running: bool = False
    timer_start_ts: Optional[float] = None  # epoch seconds when started
    timer_elapsed: float = 0.0  # accumulated seconds
    timer_duration: Optional[float] = None  # for timer mode, total seconds


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

        # Ensure relay and logical state are OFF on startup for safety
        self._state.heater_enabled = False
        self._set_relay(False)

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

        # Convert to display units based on use_imperial flag
        def _to_f(c: float) -> float:
            return c * 9.0 / 5.0 + 32.0

        use_imperial = state.get("use_imperial", True)

        if use_imperial:
            current_display = round(_to_f(state["current_temp"]), 1)
            desired_display = round(_to_f(state["desired_temp"]), 1)
            ceiling_display = round(_to_f(state["ceiling_temp"]), 1) if state.get("ceiling_temp") is not None else None
            unit_label = "F"
        else:
            current_display = round(state["current_temp"], 1)
            desired_display = round(state["desired_temp"], 1)
            ceiling_display = round(state["ceiling_temp"], 1) if state.get("ceiling_temp") is not None else None
            unit_label = "C"

        # Compute remaining confirmation time if applicable
        confirmation_remaining = None
        if state.get("confirmation_required") and state.get("confirmation_deadline"):
            remaining = state["confirmation_deadline"] - now
            if remaining > 0:
                confirmation_remaining = int(remaining)

        # High-level status for UI: Off / Standby / Heating
        if not state["heater_enabled"]:
            status = "Off"
        elif state["heater_on"]:
            status = "Heating"
        else:
            status = "Standby"

        snapshot = {
            "current_temp": current_display,
            "ceiling_temp": ceiling_display,
            "desired_temp": desired_display,
            "unit": unit_label,
            "use_imperial": use_imperial,
            "heater_enabled": state["heater_enabled"],
            "heater_on": state["heater_on"],
            "status": status,
            "heater_on_for": _fmt_duration(heater_on_duration),
            "heater_on_for_seconds": heater_on_duration,
            "time_to_setpoint": _fmt_duration(time_to_setpoint),
            "lockout_active": state.get("lockout_active", False),
            "lockout_reason": state.get("lockout_reason"),
            "confirmation_required": state.get("confirmation_required", False),
            "confirmation_remaining": confirmation_remaining,
            "scheduled_start_at": state.get("scheduled_start_at"),
            "avg_heatup_rate_c_per_sec": state.get("avg_heatup_rate_c_per_sec"),
            "price_per_kwh": state.get("price_per_kwh"),
            "heater_power_kw": state.get("heater_power_kw"),
            "timer_mode": state.get("timer_mode", "stopwatch"),
            "timer_running": state.get("timer_running", False),
            "timer_elapsed": state.get("timer_elapsed", 0.0),
            "timer_duration": state.get("timer_duration"),
        }

        # Derived cost estimate for current ON duration
        price = state.get("price_per_kwh")
        power_kw = state.get("heater_power_kw")
        if price is not None and power_kw is not None and heater_on_duration is not None:
            hours = heater_on_duration / 3600.0
            snapshot["estimated_cost_current_session"] = round(power_kw * hours * price, 2)

        return snapshot

    def set_heater_enabled(self, enabled: bool) -> None:
        """User-facing toggle: when False, heater will be forced off.

        Heater is always defaulted to OFF on startup since heater_enabled
        starts False and the relay is initialized to the OFF state.
        """
        with self._lock:
            self._state.heater_enabled = enabled

            # Clearing lockout/confirmation when user explicitly turns heater off
            if not enabled:
                self._state.lockout_active = False
                self._state.lockout_reason = None
                self._state.confirmation_required = False
                self._state.confirmation_deadline = None

                # Immediately turn relay off
                self._set_relay(False)

            self._save_state_to_disk_locked()

    def set_desired_temperature(self, temp: float) -> None:
        """Update desired temperature (in display units) and persist.

        The web UI always posts in the currently selected units. Internally we
        store and operate in Celsius, so convert if necessary.
        """
        with self._lock:
            # Convert to Celsius if currently using imperial
            if self._state.use_imperial:
                desired_c = (temp - 32.0) * 5.0 / 9.0
            else:
                desired_c = temp

            # Clamp to safety maximum
            if desired_c > MAX_TEMP_C:
                desired_c = MAX_TEMP_C

            self._state.desired_temp = desired_c
            # Reset setpoint timing if user changes the target
            self._state.time_to_setpoint = None
            self._save_state_to_disk_locked()

    def toggle_units(self) -> None:
        """Toggle between imperial (F) and metric (C) display units.

        This affects only how temperatures are shown and how new setpoints
        are interpreted; the control loop always uses Celsius internally.
        """
        with self._lock:
            self._state.use_imperial = not self._state.use_imperial
            self._save_state_to_disk_locked()

    # --- Scheduling and cost configuration API -----------------------------

    def set_schedule(self, scheduled_epoch: Optional[float]) -> None:
        """Set or clear a simple one-shot schedule.

        The timestamp is the time at which the user wants the sauna hot. The
        control loop will start heating early based on the measured
        avg_heatup_rate_c_per_sec so that the sauna is near the desired
        setpoint at that time.
        """
        with self._lock:
            self._state.scheduled_start_at = scheduled_epoch
            self._save_state_to_disk_locked()

    def set_cost_config(self, price_per_kwh: Optional[float], heater_power_kw: Optional[float]) -> None:
        """Set electricity cost and heater power for cost estimation."""
        with self._lock:
            self._state.price_per_kwh = price_per_kwh
            self._state.heater_power_kw = heater_power_kw
            self._save_state_to_disk_locked()

    def stop(self) -> None:
        """Signal the background loop to stop and cleanup GPIO."""
        self._stop_event.set()
        self._thread.join(timeout=2.0)
        GPIO.cleanup()

    # --- Timer / stopwatch API -------------------------------------------

    def timer_set_mode(self, mode: str) -> None:
        """Set timer mode to 'stopwatch' or 'timer'."""
        if mode not in {"stopwatch", "timer"}:
            return
        with self._lock:
            self._state.timer_mode = mode
            self._save_state_to_disk_locked()

    def timer_set_duration_minutes(self, minutes: int) -> None:
        """Set timer duration in minutes (only relevant in timer mode)."""
        if minutes <= 0:
            return
        with self._lock:
            self._state.timer_duration = float(minutes * 60)
            self._save_state_to_disk_locked()

    def timer_start(self) -> None:
        """Start or resume timer/stopwatch."""
        with self._lock:
            if not self._state.timer_running:
                self._state.timer_running = True
                self._state.timer_start_ts = time.time()
                self._save_state_to_disk_locked()

    def timer_stop(self) -> None:
        """Pause timer/stopwatch, accumulating elapsed time."""
        with self._lock:
            if self._state.timer_running and self._state.timer_start_ts is not None:
                now = time.time()
                self._state.timer_elapsed += now - self._state.timer_start_ts
            self._state.timer_running = False
            self._state.timer_start_ts = None
            self._save_state_to_disk_locked()

    def timer_reset(self) -> None:
        """Reset elapsed time and stop timer/stopwatch."""
        with self._lock:
            self._state.timer_running = False
            self._state.timer_start_ts = None
            self._state.timer_elapsed = 0.0
            self._save_state_to_disk_locked()

    # --- Internal helpers ---------------------------------------------------

    def _set_relay(self, on: bool) -> None:
        """Drive the relay, assuming active-HIGH behavior.

        on=True  -> GPIO HIGH -> heater ON
        on=False -> GPIO LOW  -> heater OFF
        """
        self._state.heater_on = on
        if on:
            GPIO.output(RELAY_GPIO, True)
            if self._state.heater_on_since is None:
                self._state.heater_on_since = time.time()
        else:
            GPIO.output(RELAY_GPIO, False)
            self._state.heater_on_since = None

    def _load_state_from_disk(self) -> None:
        """Load persisted configuration if present.

        Currently persisted:
        - desired_temp (Celsius)
        - heater_enabled
        - use_imperial (bool)
        """
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
            self._state.use_imperial = bool(data.get("use_imperial", self._state.use_imperial))

            # Optional persisted scheduling and cost config
            self._state.scheduled_start_at = data.get("scheduled_start_at")
            self._state.avg_heatup_rate_c_per_sec = data.get("avg_heatup_rate_c_per_sec")
            self._state.price_per_kwh = data.get("price_per_kwh")
            self._state.heater_power_kw = data.get("heater_power_kw")
            # Timer/stopwatch state (optional)
            self._state.timer_mode = data.get("timer_mode", self._state.timer_mode)
            self._state.timer_running = bool(data.get("timer_running", self._state.timer_running))
            self._state.timer_elapsed = float(data.get("timer_elapsed", self._state.timer_elapsed))
            self._state.timer_duration = data.get("timer_duration", self._state.timer_duration)
            # Safety-related flags default to safe values on startup
            self._state.lockout_active = False
            self._state.lockout_reason = None
            self._state.confirmation_required = False
            self._state.confirmation_deadline = None

    def _save_state_to_disk_locked(self) -> None:
        """Persist relevant configuration fields to JSON.

        Caller must hold self._lock.
        """
        data = {
            "desired_temp": self._state.desired_temp,
            "heater_enabled": self._state.heater_enabled,
            "use_imperial": self._state.use_imperial,
            "scheduled_start_at": self._state.scheduled_start_at,
            "avg_heatup_rate_c_per_sec": self._state.avg_heatup_rate_c_per_sec,
            "price_per_kwh": self._state.price_per_kwh,
            "heater_power_kw": self._state.heater_power_kw,
            "timer_mode": self._state.timer_mode,
            "timer_running": self._state.timer_running,
            "timer_elapsed": self._state.timer_elapsed,
            "timer_duration": self._state.timer_duration,
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
        applies dual-sensor control:
        - Bench temp (primary): drives user setpoint.
        - Ceiling temp (safety): prevents overheating near heater.
        Heater turns OFF when bench >= target OR ceiling >= 93.3°C (200°F).
        """
        CEILING_SAFETY_LIMIT_C = 93.3  # 200°F

        while not self._stop_event.is_set():
            now = time.time()
            bench_temp, ceiling_temp = read_temps()

            with self._lock:
                self._state.current_temp = bench_temp
                self._state.ceiling_temp = ceiling_temp
                self._state.last_updated = now

                desired = self._state.desired_temp
                enabled = self._state.heater_enabled

                # Enforce hard safety maximum on bench sensor
                if bench_temp >= MAX_TEMP_C:
                    self._set_relay(False)
                    self._state.lockout_active = True
                    self._state.lockout_reason = "max_temp"
                # Also enforce ceiling safety limit
                elif ceiling_temp is not None and ceiling_temp >= CEILING_SAFETY_LIMIT_C:
                    self._set_relay(False)
                    self._state.lockout_active = True
                    self._state.lockout_reason = "ceiling_overtemp"
                else:
                    # Simple one-shot schedule: if we have a scheduled target
                    # time and an estimate of heat-up rate, start early enough
                    # to reach desired temp by scheduled_start_at.
                    if (
                        self._state.scheduled_start_at is not None
                        and self._state.avg_heatup_rate_c_per_sec is not None
                        and not self._state.lockout_active
                    ):
                        time_until_target = self._state.scheduled_start_at - now
                        # How long we expect to need to heat from current temp
                        delta_c = max(desired - bench_temp, 0.0)
                        if self._state.avg_heatup_rate_c_per_sec > 0:
                            required_heat_time = delta_c / self._state.avg_heatup_rate_c_per_sec
                        else:
                            required_heat_time = 0.0

                        # If it's time to start heating (or we're late), enable heater
                        if time_until_target <= required_heat_time:
                            self._state.heater_enabled = True
                            enabled = True
                            # one-shot schedule; clear once we have started
                            self._state.scheduled_start_at = None

                    # Handle max-on-time safety
                    if self._state.heater_on_since is not None:
                        on_duration = now - self._state.heater_on_since
                        if (
                            on_duration >= MAX_ON_TIME_SEC
                            and not self._state.confirmation_required
                            and not self._state.lockout_active
                        ):
                            # Start confirmation window
                            self._state.confirmation_required = True
                            self._state.confirmation_deadline = now + CONFIRMATION_TIMEOUT_SEC

                    # If confirmation window has expired without user action
                    if (
                        self._state.confirmation_required
                        and self._state.confirmation_deadline is not None
                        and now >= self._state.confirmation_deadline
                    ):
                        self._set_relay(False)
                        self._state.heater_enabled = False
                        self._state.confirmation_required = False
                        self._state.confirmation_deadline = None
                        self._state.lockout_active = True
                        self._state.lockout_reason = "max_on_time"

                    # Dual-sensor bang-bang control:
                    # Turn ON if bench < target - hysteresis AND ceiling < safety limit.
                    # Turn OFF if bench > target + hysteresis OR ceiling >= safety limit - hysteresis.
                    if not self._state.lockout_active and enabled:
                        # Check if we should turn heater ON
                        bench_wants_heat = bench_temp < desired - HYSTERESIS
                        ceiling_safe = (ceiling_temp is None or
                                        ceiling_temp < CEILING_SAFETY_LIMIT_C - HYSTERESIS)

                        # Check if we should turn heater OFF
                        bench_hot_enough = bench_temp > desired + HYSTERESIS
                        ceiling_too_hot = (ceiling_temp is not None and
                                           ceiling_temp >= CEILING_SAFETY_LIMIT_C - HYSTERESIS)

                        if bench_wants_heat and ceiling_safe:
                            self._set_relay(True)
                        elif bench_hot_enough or ceiling_too_hot:
                            self._set_relay(False)
                    else:
                        # If not enabled or in lockout, ensure relay is off
                        if self._state.heater_on:
                            self._set_relay(False)

                # Track time to first reach setpoint and update average heatup rate
                if self._state.heater_on_since is not None and bench_temp >= desired:
                    elapsed = now - self._state.heater_on_since
                    if elapsed > 0:
                        if self._state.time_to_setpoint is None:
                            # First time we hit setpoint for this session
                            self._state.time_to_setpoint = elapsed

                        # Update average heat-up rate (simple exponential moving average)
                        delta_c = max(desired - self._state.current_temp + (bench_temp - desired), 0.0)
                        # Conservative: use desired - (bench_temp at heater_on_since approx)
                        # If we assume heater_on_since temp was lower, approximate delta_c
                        if delta_c <= 0:
                            delta_c = desired - (bench_temp - 5.0)
                        if delta_c > 0:
                            new_rate = delta_c / elapsed
                            old_rate = self._state.avg_heatup_rate_c_per_sec
                            if old_rate is None:
                                self._state.avg_heatup_rate_c_per_sec = new_rate
                            else:
                                alpha = 0.3
                                self._state.avg_heatup_rate_c_per_sec = (
                                    alpha * new_rate + (1 - alpha) * old_rate
                                )

                # Update timer/stopwatch elapsed time when running
                if self._state.timer_running and self._state.timer_start_ts is not None:
                    self._state.timer_elapsed += CONTROL_INTERVAL_SEC

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
