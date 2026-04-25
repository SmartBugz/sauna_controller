"""Background logic for the smart sauna controller.

Responsibilities:
- Read temperature from DS18B20 1-Wire sensors or MAX31855 thermocouple sensors
- Control a GPIO relay driving the sauna heater
- Maintain shared state (temps, setpoints, on/off status, timings)
- Persist configuration to JSON
- Run a background bang-bang hysteresis control loop
- Optional Home Assistant integration via MQTT auto-discovery

Thermometer Modes
-----------------
single  One sensor at any standard placement. The 100 C physical thermal
        switch provides hardware safety. The software adds a MAX_TEMP_C lockout.

dual    Two sensors. The "goal" sensor (bench height) drives the setpoint.
        The "limit" sensor (ceiling or heater area) enforces a user-configurable
        upper cutoff. Example: heat until bench = 82 C (180 F) but shut the
        heater off any time the ceiling sensor reads >= 93 C (200 F).

Sensor Types
------------
ds18b20       1-Wire DS18B20 digital thermometer (default, recommended).
              Stainless-steel-tipped probes are available for high-heat use.
thermocouple  MAX31855 K-type thermocouple via SPI. Use when coating
              off-gassing is a concern and a high-heat-rated probe is required.

This module runs on Raspberry Pi (real GPIO + real sensors) and on a standard
PC for development (mock GPIO + synthetic temperature values).
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from dataclasses import asdict, dataclass
from typing import Optional

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "sauna_state.json")

# GPIO pin (BCM numbering)
RELAY_GPIO = 17

# Control constants
HYSTERESIS = 2.5
MAX_TEMP_C = 105.0
SESSION_MAX_DURATION_SEC = 4 * 60 * 60
SLOW_HEATUP_AFTER_SEC = 15 * 60
SLOW_HEATUP_TEMP_DELTA_C = 5.0
CONTROL_INTERVAL_SEC = 2.0
MQTT_PUBLISH_INTERVAL_SEC = 5.0

DEFAULT_DESIRED_TEMP_C = 70.0
DEFAULT_LIMIT_TEMP_C = 93.3


# -- GPIO ---------------------------------------------------------------------

class BaseGPIO:
    BCM = "BCM"
    OUT = "OUT"

    def setmode(self, mode):
        pass

    def setup(self, pin, mode):
        pass

    def output(self, pin, value):
        pass

    def cleanup(self):
        pass


class MockGPIO(BaseGPIO):
    """In-memory GPIO mock for development on non-Pi hardware."""

    def __init__(self):
        self.state: dict[int, bool] = {}

    def setmode(self, mode):
        self.mode = mode

    def setup(self, pin, mode):
        self.state[pin] = False

    def output(self, pin, value):
        self.state[pin] = bool(value)

    def cleanup(self):
        self.state.clear()


try:
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
except Exception:
    GPIO = MockGPIO()


# -- Thermocouple (MAX31855) --------------------------------------------------

try:
    import adafruit_max31855  # type: ignore
    import board  # type: ignore
    import busio  # type: ignore
    import digitalio  # type: ignore

    _THERMO_AVAILABLE = True
except ImportError:
    _THERMO_AVAILABLE = False

_thermo_locks: dict[int, threading.Lock] = {}
_thermo_sensors: dict[int, object] = {}


def _read_thermocouple(cs_gpio_pin: int) -> Optional[float]:
    """Read C from MAX31855 at the given CS pin. Returns None on error."""
    if not _THERMO_AVAILABLE:
        return None
    if cs_gpio_pin not in _thermo_locks:
        _thermo_locks[cs_gpio_pin] = threading.Lock()

    with _thermo_locks[cs_gpio_pin]:
        try:
            if cs_gpio_pin not in _thermo_sensors:
                cs = digitalio.DigitalInOut(getattr(board, f"D{cs_gpio_pin}"))
                spi = busio.SPI(board.SCK, MOSI=board.MOSI, MISO=board.MISO)
                _thermo_sensors[cs_gpio_pin] = adafruit_max31855.MAX31855(spi, cs)
            return float(_thermo_sensors[cs_gpio_pin].temperature)
        except Exception:
            _thermo_sensors.pop(cs_gpio_pin, None)
            return None


# -- DS18B20 (1-Wire) ---------------------------------------------------------

def _find_all_w1_devices() -> dict[str, str]:
    base_dir = "/sys/bus/w1/devices"
    if not os.path.isdir(base_dir):
        return {}

    devices: dict[str, str] = {}
    for name in os.listdir(base_dir):
        if name.startswith("28-"):
            device_file = os.path.join(base_dir, name, "w1_slave")
            if os.path.isfile(device_file):
                devices[name] = device_file
    return devices


W1_DEVICES = _find_all_w1_devices()


def _read_ds18b20(device_path: str) -> Optional[float]:
    try:
        with open(device_path, "r", encoding="utf-8") as file:
            lines = file.readlines()
        if len(lines) >= 2 and "YES" in lines[0] and "t=" in lines[1]:
            return float(lines[1].split("t=")[-1].strip()) / 1000.0
    except Exception:
        pass
    return None


def _read_sensor(
    sensor_type: str,
    sensor_id: Optional[str],
    spi_cs_pin: int,
    use_first_w1_fallback: bool = False,
) -> Optional[float]:
    if sensor_type == "thermocouple":
        return _read_thermocouple(spi_cs_pin)

    if W1_DEVICES:
        if sensor_id and sensor_id in W1_DEVICES:
            return _read_ds18b20(W1_DEVICES[sensor_id])
        if use_first_w1_fallback:
            return _read_ds18b20(next(iter(W1_DEVICES.values())))
    return None


_mock_state = {"goal_temp": 45.0, "limit_temp": 50.0}

def _mock_temps(heater_on: bool) -> tuple[float, float]:
    """Simulate temperature rise when heater is ON, fall when OFF."""
    if heater_on:
        _mock_state["goal_temp"] += 0.5
        _mock_state["limit_temp"] += 0.3
    else:
        _mock_state["goal_temp"] -= 0.3
        _mock_state["limit_temp"] -= 0.2
    _mock_state["goal_temp"] = max(20.0, min(100.0, _mock_state["goal_temp"]))
    _mock_state["limit_temp"] = max(25.0, min(105.0, _mock_state["limit_temp"]))
    return _mock_state["goal_temp"], _mock_state["limit_temp"]


# -- Shared state -------------------------------------------------------------

@dataclass
class SaunaState:
    # Live sensor readings
    current_temp: float = 0.0
    limit_sensor_temp: Optional[float] = None

    # Setpoints in C
    desired_temp: float = DEFAULT_DESIRED_TEMP_C
    limit_temp: float = DEFAULT_LIMIT_TEMP_C

    # Heater state
    heater_enabled: bool = False
    heater_on: bool = False
    heater_on_since: Optional[float] = None
    time_to_setpoint: Optional[float] = None
    session_started_at: Optional[float] = None
    session_start_temp_c: Optional[float] = None
    slow_heat_alert_sent: bool = False

    # Safety
    lockout_active: bool = False
    lockout_reason: Optional[str] = None
    confirmation_required: bool = False
    confirmation_deadline: Optional[float] = None
    last_event_type: Optional[str] = None
    last_event_message: Optional[str] = None
    last_event_ts: Optional[float] = None

    # Display
    use_imperial: bool = True
    last_updated: Optional[float] = None

    # Scheduling
    scheduled_start_at: Optional[float] = None
    avg_heatup_rate_c_per_sec: Optional[float] = None

    # Cost tracking
    price_per_kwh: Optional[float] = None
    heater_power_kw: Optional[float] = None

    # Timer / stopwatch
    timer_mode: str = "stopwatch"
    timer_running: bool = False
    timer_start_ts: Optional[float] = None
    timer_elapsed: float = 0.0
    timer_duration: Optional[float] = None

    # Thermometer configuration
    thermometer_mode: str = "single"
    sensor_type: str = "ds18b20"
    goal_sensor_type: str = "ds18b20"
    limit_sensor_type: str = "ds18b20"
    bench_sensor_id: Optional[str] = None
    ceiling_sensor_id: Optional[str] = None
    goal_spi_cs: int = 8
    limit_spi_cs: int = 7

    # MQTT / Home Assistant
    mqtt_enabled: bool = False
    mqtt_broker: str = ""
    mqtt_port: int = 1883
    mqtt_username: str = ""
    mqtt_password: str = ""


# -- MQTT ---------------------------------------------------------------------

try:
    import paho.mqtt.client as _paho  # type: ignore

    _MQTT_AVAILABLE = True
except ImportError:
    _MQTT_AVAILABLE = False


class MQTTPublisher:
    """MQTT publisher with Home Assistant MQTT discovery support."""

    _ID = "sauna_controller"
    _STATE_TOPIC = f"{_ID}/state"
    _AVAIL_TOPIC = f"{_ID}/availability"
    _EVENT_TOPIC = f"{_ID}/event"
    _CMD_MODE = f"{_ID}/cmd/mode"
    _CMD_SETPOINT = f"{_ID}/cmd/setpoint"
    _CMD_LIMIT = f"{_ID}/cmd/limit_temp"

    def __init__(self, controller: "SaunaController") -> None:
        self._ctrl = controller
        self._client = None
        self.connected = False

    def start(self, broker: str, port: int, username: str, password: str) -> None:
        if not _MQTT_AVAILABLE or not broker:
            if not _MQTT_AVAILABLE:
                print("[MQTT] paho-mqtt not installed; MQTT disabled.")
            return
        try:
            self._client = _paho.Client(client_id=self._ID, clean_session=False)
            if username:
                self._client.username_pw_set(username, password or "")
            self._client.will_set(self._AVAIL_TOPIC, "offline", qos=1, retain=True)
            self._client.on_connect = self._on_connect
            self._client.on_message = self._on_message
            self._client.on_disconnect = self._on_disconnect
            self._client.connect_async(broker, port, keepalive=60)
            self._client.loop_start()
        except Exception as exc:
            print(f"[MQTT] Startup error: {exc}")

    def stop(self) -> None:
        if self._client:
            try:
                self._client.publish(self._AVAIL_TOPIC, "offline", qos=1, retain=True)
                self._client.loop_stop()
                self._client.disconnect()
            except Exception:
                pass
        self.connected = False

    def publish_state(self, snap: dict) -> None:
        if not self._client or not self.connected:
            return
        payload = {
            "current_temp_c": snap.get("current_temp_c"),
            "setpoint_c": snap.get("desired_temp_c"),
            "limit_sensor_temp_c": snap.get("limit_sensor_temp_c"),
            "limit_temp_c": snap.get("limit_temp_c"),
            "heater_on": bool(snap.get("heater_on")),
            "lockout_active": bool(snap.get("lockout_active")),
            "lockout_reason": snap.get("lockout_reason") or "none",
            "session_elapsed_seconds": snap.get("session_elapsed_seconds") or 0,
            "session_remaining_seconds": snap.get("session_remaining_seconds") or 0,
            "last_event_type": snap.get("last_event_type") or "none",
            "last_event_message": snap.get("last_event_message") or "",
            "mode": "heat" if snap.get("heater_enabled") else "off",
            "action": (
                "heating"
                if snap.get("heater_on")
                else "idle"
                if snap.get("heater_enabled")
                else "off"
            ),
            "thermometer_mode": snap.get("thermometer_mode"),
        }
        try:
            self._client.publish(self._STATE_TOPIC, json.dumps(payload), qos=0, retain=True)
            self._client.publish(self._AVAIL_TOPIC, "online", qos=1, retain=True)
        except Exception:
            pass

    def publish_event(self, payload: dict) -> None:
        if not self._client or not self.connected:
            return
        try:
            self._client.publish(self._EVENT_TOPIC, json.dumps(payload), qos=1, retain=False)
        except Exception:
            pass

    def _on_connect(self, client, userdata, flags, rc) -> None:
        if rc != 0:
            self.connected = False
            print(f"[MQTT] Connect failed rc={rc}")
            return
        self.connected = True
        client.publish(self._AVAIL_TOPIC, "online", qos=1, retain=True)
        client.subscribe([(self._CMD_MODE, 0), (self._CMD_SETPOINT, 0), (self._CMD_LIMIT, 0)])
        self._publish_discovery(client)

    def _on_disconnect(self, client, userdata, rc) -> None:
        self.connected = False

    def _on_message(self, client, userdata, msg) -> None:
        topic = msg.topic
        raw = msg.payload.decode("utf-8", errors="replace").strip()
        if topic == self._CMD_MODE:
            if raw == "heat":
                self._ctrl.set_heater_enabled(True)
            elif raw == "off":
                self._ctrl.set_heater_enabled(False)
        elif topic == self._CMD_SETPOINT:
            try:
                self._ctrl.set_desired_temperature_c(float(raw))
            except (TypeError, ValueError):
                pass
        elif topic == self._CMD_LIMIT:
            try:
                self._ctrl.set_limit_temp_c(float(raw))
            except (TypeError, ValueError):
                pass

    def _publish_discovery(self, client) -> None:
        device = {
            "identifiers": [self._ID],
            "name": "Sauna Controller",
            "model": "Smart Sauna Controller",
            "manufacturer": "DIY",
        }
        climate_cfg = {
            "name": "Sauna",
            "unique_id": f"{self._ID}_climate",
            "device": device,
            "modes": ["off", "heat"],
            "current_temperature_topic": self._STATE_TOPIC,
            "current_temperature_template": "{{ value_json.current_temp_c }}",
            "temperature_command_topic": self._CMD_SETPOINT,
            "temperature_state_topic": self._STATE_TOPIC,
            "temperature_state_template": "{{ value_json.setpoint_c }}",
            "mode_command_topic": self._CMD_MODE,
            "mode_state_topic": self._STATE_TOPIC,
            "mode_state_template": "{{ value_json.mode }}",
            "action_topic": self._STATE_TOPIC,
            "action_template": "{{ value_json.action }}",
            "min_temp": 40,
            "max_temp": 100,
            "temp_step": 0.5,
            "temperature_unit": "C",
            "availability_topic": self._AVAIL_TOPIC,
        }
        limit_sensor_cfg = {
            "name": "Sauna Limit Sensor",
            "unique_id": f"{self._ID}_limit_sensor",
            "device": device,
            "device_class": "temperature",
            "state_topic": self._STATE_TOPIC,
            "value_template": "{{ value_json.limit_sensor_temp_c }}",
            "unit_of_measurement": "C",
            "availability_topic": self._AVAIL_TOPIC,
        }
        heater_relay_cfg = {
            "name": "Sauna Heater Relay",
            "unique_id": f"{self._ID}_heater_relay",
            "device": device,
            "state_topic": self._STATE_TOPIC,
            "value_template": "{{ 'ON' if value_json.heater_on else 'OFF' }}",
            "payload_on": "ON",
            "payload_off": "OFF",
            "availability_topic": self._AVAIL_TOPIC,
        }
        safety_lockout_cfg = {
            "name": "Sauna Safety Lockout",
            "unique_id": f"{self._ID}_safety_lockout",
            "device": device,
            "device_class": "problem",
            "state_topic": self._STATE_TOPIC,
            "value_template": "{{ 'ON' if value_json.lockout_active else 'OFF' }}",
            "payload_on": "ON",
            "payload_off": "OFF",
            "availability_topic": self._AVAIL_TOPIC,
        }
        lockout_reason_cfg = {
            "name": "Sauna Lockout Reason",
            "unique_id": f"{self._ID}_lockout_reason",
            "device": device,
            "state_topic": self._STATE_TOPIC,
            "value_template": "{{ value_json.lockout_reason }}",
            "entity_category": "diagnostic",
            "icon": "mdi:shield-alert-outline",
            "availability_topic": self._AVAIL_TOPIC,
        }
        session_elapsed_cfg = {
            "name": "Sauna Session Elapsed",
            "unique_id": f"{self._ID}_session_elapsed",
            "device": device,
            "state_topic": self._STATE_TOPIC,
            "value_template": "{{ value_json.session_elapsed_seconds }}",
            "device_class": "duration",
            "unit_of_measurement": "s",
            "availability_topic": self._AVAIL_TOPIC,
        }
        session_remaining_cfg = {
            "name": "Sauna Session Remaining",
            "unique_id": f"{self._ID}_session_remaining",
            "device": device,
            "state_topic": self._STATE_TOPIC,
            "value_template": "{{ value_json.session_remaining_seconds }}",
            "device_class": "duration",
            "unit_of_measurement": "s",
            "availability_topic": self._AVAIL_TOPIC,
        }
        last_event_cfg = {
            "name": "Sauna Last Event",
            "unique_id": f"{self._ID}_last_event",
            "device": device,
            "state_topic": self._STATE_TOPIC,
            "value_template": "{{ value_json.last_event_type }}",
            "entity_category": "diagnostic",
            "icon": "mdi:bell-alert-outline",
            "availability_topic": self._AVAIL_TOPIC,
        }
        last_event_message_cfg = {
            "name": "Sauna Last Event Message",
            "unique_id": f"{self._ID}_last_event_message",
            "device": device,
            "state_topic": self._STATE_TOPIC,
            "value_template": "{{ value_json.last_event_message }}",
            "entity_category": "diagnostic",
            "icon": "mdi:message-alert-outline",
            "availability_topic": self._AVAIL_TOPIC,
        }

        client.publish(
            f"homeassistant/climate/{self._ID}/config",
            json.dumps(climate_cfg),
            qos=1,
            retain=True,
        )
        client.publish(
            f"homeassistant/sensor/{self._ID}_limit/config",
            json.dumps(limit_sensor_cfg),
            qos=1,
            retain=True,
        )
        client.publish(
            f"homeassistant/binary_sensor/{self._ID}_heater_relay/config",
            json.dumps(heater_relay_cfg),
            qos=1,
            retain=True,
        )
        client.publish(
            f"homeassistant/binary_sensor/{self._ID}_safety_lockout/config",
            json.dumps(safety_lockout_cfg),
            qos=1,
            retain=True,
        )
        client.publish(
            f"homeassistant/sensor/{self._ID}_lockout_reason/config",
            json.dumps(lockout_reason_cfg),
            qos=1,
            retain=True,
        )
        client.publish(
            f"homeassistant/sensor/{self._ID}_session_elapsed/config",
            json.dumps(session_elapsed_cfg),
            qos=1,
            retain=True,
        )
        client.publish(
            f"homeassistant/sensor/{self._ID}_session_remaining/config",
            json.dumps(session_remaining_cfg),
            qos=1,
            retain=True,
        )
        client.publish(
            f"homeassistant/sensor/{self._ID}_last_event/config",
            json.dumps(last_event_cfg),
            qos=1,
            retain=True,
        )
        client.publish(
            f"homeassistant/sensor/{self._ID}_last_event_message/config",
            json.dumps(last_event_message_cfg),
            qos=1,
            retain=True,
        )


# -- Controller ---------------------------------------------------------------

class SaunaController:
    """Manages background reading and heater control in its own thread."""

    def __init__(self, config_path: str = CONFIG_PATH):
        self.config_path = config_path
        self.repo_path = os.path.dirname(os.path.abspath(config_path))
        self._git_branch, self._git_commit, self._git_dirty = self._read_git_version()
        self._lock = threading.Lock()
        self._state = SaunaState()
        self._load_state_from_disk()

        GPIO.setmode(GPIO.BCM)
        GPIO.setup(RELAY_GPIO, GPIO.OUT)

        # Always start safe
        self._state.heater_enabled = False
        self._set_relay(False)

        if W1_DEVICES:
            print("\n=== Detected DS18B20 Sensors ===")
            for sensor_id in W1_DEVICES:
                print(f"  {sensor_id}")
            print("================================\n")

        self._mqtt = MQTTPublisher(self)
        if self._state.mqtt_enabled and self._state.mqtt_broker:
            self._mqtt.start(
                self._state.mqtt_broker,
                self._state.mqtt_port,
                self._state.mqtt_username,
                self._state.mqtt_password,
            )

        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    # -- Public API -----------------------------------------------------------

    def get_state_snapshot(self) -> dict:
        with self._lock:
            state = asdict(self._state)

        now = time.time()
        use_imperial = bool(state.get("use_imperial", True))

        def to_f(temp_c: float) -> float:
            return temp_c * 9.0 / 5.0 + 32.0

        def display(temp_c: Optional[float]) -> Optional[float]:
            if temp_c is None:
                return None
            value = to_f(temp_c) if use_imperial else temp_c
            return round(value, 1)

        def fmt_duration(seconds: Optional[float]) -> Optional[str]:
            if seconds is None:
                return None
            total = int(seconds)
            hours, rem = divmod(total, 3600)
            minutes, sec = divmod(rem, 60)
            return f"{hours:02d}:{minutes:02d}:{sec:02d}"

        heater_on_duration = None
        if state.get("heater_on") and state.get("heater_on_since"):
            heater_on_duration = now - float(state["heater_on_since"])

        session_elapsed = None
        session_remaining = None
        if state.get("session_started_at"):
            session_elapsed = max(now - float(state["session_started_at"]), 0.0)
            session_remaining = max(SESSION_MAX_DURATION_SEC - session_elapsed, 0.0)

        confirmation_remaining = None
        if state.get("confirmation_required") and state.get("confirmation_deadline"):
            remain = float(state["confirmation_deadline"]) - now
            if remain > 0:
                confirmation_remaining = int(remain)

        if not state.get("heater_enabled"):
            status = "Off"
        elif state.get("heater_on"):
            status = "Heating"
        else:
            status = "Standby"

        snapshot = {
            "current_temp_c": round(float(state["current_temp"]), 2),
            "desired_temp_c": round(float(state["desired_temp"]), 2),
            "limit_sensor_temp_c": (
                round(float(state["limit_sensor_temp"]), 2)
                if state.get("limit_sensor_temp") is not None
                else None
            ),
            "limit_temp_c": round(float(state["limit_temp"]), 2),
            "current_temp": display(state.get("current_temp")),
            "desired_temp": display(state.get("desired_temp")),
            "limit_sensor_temp": display(state.get("limit_sensor_temp")),
            "limit_temp": display(state.get("limit_temp")),
            "unit": "F" if use_imperial else "C",
            "use_imperial": use_imperial,
            "heater_enabled": bool(state.get("heater_enabled")),
            "heater_on": bool(state.get("heater_on")),
            "status": status,
            "heater_on_for": fmt_duration(heater_on_duration),
            "heater_on_for_seconds": heater_on_duration,
            "session_elapsed": fmt_duration(session_elapsed),
            "session_elapsed_seconds": session_elapsed,
            "session_remaining": fmt_duration(session_remaining),
            "session_remaining_seconds": session_remaining,
            "session_start_temp_c": (
                round(float(state["session_start_temp_c"]), 2)
                if state.get("session_start_temp_c") is not None
                else None
            ),
            "time_to_setpoint": fmt_duration(state.get("time_to_setpoint")),
            "lockout_active": bool(state.get("lockout_active", False)),
            "lockout_reason": state.get("lockout_reason"),
            "confirmation_required": bool(state.get("confirmation_required", False)),
            "confirmation_remaining": confirmation_remaining,
            "last_event_type": state.get("last_event_type"),
            "last_event_message": state.get("last_event_message"),
            "last_event_ts": state.get("last_event_ts"),
            "scheduled_start_at": state.get("scheduled_start_at"),
            "avg_heatup_rate_c_per_sec": state.get("avg_heatup_rate_c_per_sec"),
            "price_per_kwh": state.get("price_per_kwh"),
            "heater_power_kw": state.get("heater_power_kw"),
            "timer_mode": state.get("timer_mode", "stopwatch"),
            "timer_running": bool(state.get("timer_running", False)),
            "timer_elapsed": float(state.get("timer_elapsed", 0.0)),
            "timer_duration": state.get("timer_duration"),
            "thermometer_mode": state.get("thermometer_mode", "single"),
            "sensor_type": state.get("sensor_type", "ds18b20"),
            "bench_sensor_id": state.get("bench_sensor_id"),
            "ceiling_sensor_id": state.get("ceiling_sensor_id"),
            "goal_spi_cs": int(state.get("goal_spi_cs", 8)),
            "limit_spi_cs": int(state.get("limit_spi_cs", 7)),
            "detected_sensors": list(W1_DEVICES.keys()),
            "mqtt_enabled": bool(state.get("mqtt_enabled", False)),
            "mqtt_broker": state.get("mqtt_broker", ""),
            "mqtt_port": int(state.get("mqtt_port", 1883)),
            "mqtt_username": state.get("mqtt_username", ""),
            "mqtt_connected": self._mqtt.connected,
            "session_max_duration_seconds": SESSION_MAX_DURATION_SEC,
            "app_git_branch": self._git_branch,
            "app_git_commit": self._git_commit,
            "app_git_dirty": self._git_dirty,
            "kiosk_autostart_enabled": self.kiosk_autostart_enabled(),
        }

        price = state.get("price_per_kwh")
        power_kw = state.get("heater_power_kw")
        if price is not None and power_kw is not None and heater_on_duration is not None:
            snapshot["estimated_cost_current_session"] = round(
                float(power_kw) * (heater_on_duration / 3600.0) * float(price),
                2,
            )

        return snapshot

    def _kiosk_autostart_path(self) -> str:
        return os.path.join(os.path.expanduser("~"), ".config", "autostart", "smart-sauna-kiosk.desktop")

    def kiosk_autostart_enabled(self) -> bool:
        return os.path.isfile(self._kiosk_autostart_path())

    def set_kiosk_autostart(self, enabled: bool) -> None:
        autostart_path = self._kiosk_autostart_path()
        autostart_dir = os.path.dirname(autostart_path)

        if enabled:
            os.makedirs(autostart_dir, exist_ok=True)
            content = (
                "[Desktop Entry]\n"
                "Type=Application\n"
                "Version=1.0\n"
                "Name=Smart Sauna Kiosk\n"
                "Comment=Launch Smart Sauna in kiosk mode on login\n"
                f"Exec={self.repo_path}/launch_sauna_kiosk.sh\n"
                "Icon=web-browser\n"
                "Terminal=false\n"
                "Categories=Utility;\n"
            )
            with open(autostart_path, "w", encoding="utf-8", newline="\n") as file:
                file.write(content)
            try:
                os.chmod(autostart_path, 0o755)
            except Exception:
                pass
        else:
            try:
                os.remove(autostart_path)
            except FileNotFoundError:
                pass

    def set_heater_enabled(self, enabled: bool) -> None:
        event = None
        with self._lock:
            was_enabled = bool(self._state.heater_enabled)
            self._state.heater_enabled = bool(enabled)
            if enabled and not was_enabled:
                self._state.lockout_active = False
                self._state.lockout_reason = None
                self._state.confirmation_required = False
                self._state.confirmation_deadline = None
                self._state.session_started_at = time.time()
                self._state.session_start_temp_c = float(self._state.current_temp)
                self._state.slow_heat_alert_sent = False
                self._state.time_to_setpoint = None
                event = self._record_event_locked(
                    "session_started",
                    "Sauna session started.",
                    session_elapsed_seconds=0,
                    session_start_temp_c=round(float(self._state.current_temp), 2),
                )
            elif not enabled:
                session_elapsed = None
                if self._state.session_started_at is not None:
                    session_elapsed = max(time.time() - self._state.session_started_at, 0.0)
                self._state.lockout_active = False
                self._state.lockout_reason = None
                self._state.confirmation_required = False
                self._state.confirmation_deadline = None
                self._state.session_started_at = None
                self._state.session_start_temp_c = None
                self._state.slow_heat_alert_sent = False
                self._set_relay(False)
                if was_enabled:
                    event = self._record_event_locked(
                        "session_stopped",
                        "Sauna session stopped.",
                        session_elapsed_seconds=round(session_elapsed or 0.0, 1),
                    )
            self._save_state_to_disk_locked()

        if event is not None:
            self._mqtt.publish_event(event)

    def confirm_continue(self) -> None:
        self.set_heater_enabled(True)

    def set_desired_temperature(self, temp: float) -> None:
        with self._lock:
            if self._state.use_imperial:
                desired_c = (temp - 32.0) * 5.0 / 9.0
            else:
                desired_c = temp
            self._state.desired_temp = min(float(desired_c), MAX_TEMP_C - 5.0)
            if self._state.heater_on and self._state.current_temp >= self._state.desired_temp:
                self._set_relay(False)
            self._state.time_to_setpoint = None
            self._save_state_to_disk_locked()

    def set_desired_temperature_c(self, temp_c: float) -> None:
        with self._lock:
            self._state.desired_temp = min(float(temp_c), MAX_TEMP_C - 5.0)
            if self._state.heater_on and self._state.current_temp >= self._state.desired_temp:
                self._set_relay(False)
            self._state.time_to_setpoint = None
            self._save_state_to_disk_locked()

    def set_limit_temp(self, temp: float) -> None:
        with self._lock:
            if self._state.use_imperial:
                limit_c = (temp - 32.0) * 5.0 / 9.0
            else:
                limit_c = temp
            self._state.limit_temp = min(float(limit_c), MAX_TEMP_C - 5.0)
            self._save_state_to_disk_locked()

    def set_limit_temp_c(self, temp_c: float) -> None:
        with self._lock:
            self._state.limit_temp = min(float(temp_c), MAX_TEMP_C - 5.0)
            self._save_state_to_disk_locked()

    def set_thermometer_mode(self, mode: str) -> None:
        if mode not in ("single", "dual"):
            return
        with self._lock:
            self._state.thermometer_mode = mode
            self._save_state_to_disk_locked()

    def set_goal_sensor_type(self, sensor_type: str) -> None:
        """Set goal sensor type (ds18b20 or thermocouple)."""
        if sensor_type not in ("ds18b20", "thermocouple"):
            return
        with self._lock:
            self._state.goal_sensor_type = sensor_type
            self._save_state_to_disk_locked()
    
    def set_limit_sensor_type(self, sensor_type: str) -> None:
        """Set limit sensor type (ds18b20 or thermocouple)."""
        if sensor_type not in ("ds18b20", "thermocouple"):
            return
        with self._lock:
            self._state.limit_sensor_type = sensor_type
            self._save_state_to_disk_locked()

    def set_sensor_type(self, sensor_type: str) -> None:
        """DEPRECATED: Use set_goal_sensor_type / set_limit_sensor_type instead."""
        if sensor_type not in ("ds18b20", "thermocouple"):
            return
        with self._lock:
            self._state.sensor_type = sensor_type
            self._state.goal_sensor_type = sensor_type
            self._state.limit_sensor_type = sensor_type
            self._save_state_to_disk_locked()

    def set_sensor_ids(self, bench_id: Optional[str], ceiling_id: Optional[str]) -> None:
        with self._lock:
            self._state.bench_sensor_id = bench_id or None
            self._state.ceiling_sensor_id = ceiling_id or None
            self._save_state_to_disk_locked()

    def set_spi_pins(self, goal_cs: int, limit_cs: int) -> None:
        with self._lock:
            self._state.goal_spi_cs = int(goal_cs)
            self._state.limit_spi_cs = int(limit_cs)
            self._save_state_to_disk_locked()

    def set_mqtt_config(
        self,
        enabled: bool,
        broker: str,
        port: int,
        username: str,
        password: str,
    ) -> None:
        with self._lock:
            changed = (
                self._state.mqtt_enabled != enabled
                or self._state.mqtt_broker != broker
                or self._state.mqtt_port != int(port)
                or self._state.mqtt_username != username
                or self._state.mqtt_password != password
            )
            self._state.mqtt_enabled = bool(enabled)
            self._state.mqtt_broker = broker
            self._state.mqtt_port = int(port)
            self._state.mqtt_username = username
            self._state.mqtt_password = password
            self._save_state_to_disk_locked()

        if changed:
            self._mqtt.stop()
            if enabled and broker:
                self._mqtt.start(broker, int(port), username, password)

    def toggle_units(self) -> None:
        with self._lock:
            self._state.use_imperial = not self._state.use_imperial
            self._save_state_to_disk_locked()

    def set_schedule(self, scheduled_epoch: Optional[float]) -> None:
        with self._lock:
            self._state.scheduled_start_at = scheduled_epoch
            self._save_state_to_disk_locked()

    def set_cost_config(self, price_per_kwh: Optional[float], heater_power_kw: Optional[float]) -> None:
        with self._lock:
            self._state.price_per_kwh = price_per_kwh
            self._state.heater_power_kw = heater_power_kw
            self._save_state_to_disk_locked()

    def stop(self) -> None:
        self._stop_event.set()
        self._thread.join(timeout=2.0)
        self._mqtt.stop()
        GPIO.cleanup()

    # -- Timer API ------------------------------------------------------------

    def timer_set_mode(self, mode: str) -> None:
        if mode not in {"stopwatch", "timer"}:
            return
        with self._lock:
            self._state.timer_mode = mode
            self._save_state_to_disk_locked()

    def timer_set_duration_minutes(self, minutes: int) -> None:
        if minutes <= 0:
            return
        with self._lock:
            self._state.timer_duration = float(minutes * 60)
            self._save_state_to_disk_locked()

    def timer_start(self) -> None:
        with self._lock:
            if not self._state.timer_running:
                self._state.timer_running = True
                self._state.timer_start_ts = time.time()
                self._save_state_to_disk_locked()

    def timer_stop(self) -> None:
        with self._lock:
            if self._state.timer_running and self._state.timer_start_ts is not None:
                self._state.timer_elapsed += time.time() - self._state.timer_start_ts
            self._state.timer_running = False
            self._state.timer_start_ts = None
            self._save_state_to_disk_locked()

    def timer_reset(self) -> None:
        with self._lock:
            self._state.timer_running = False
            self._state.timer_start_ts = None
            self._state.timer_elapsed = 0.0
            self._save_state_to_disk_locked()

    # -- Internal -------------------------------------------------------------

    def _read_git_version(self) -> tuple[str, str, bool]:
        branch = "unknown"
        commit = "unknown"
        dirty = False

        try:
            branch = subprocess.check_output(
                ["git", "-C", self.repo_path, "symbolic-ref", "--short", "HEAD"],
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
        except Exception:
            pass

        try:
            commit = subprocess.check_output(
                ["git", "-C", self.repo_path, "rev-parse", "--short", "HEAD"],
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
        except Exception:
            pass

        try:
            dirty = (
                subprocess.run(
                    ["git", "-C", self.repo_path, "diff", "--quiet"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                ).returncode
                != 0
            )
        except Exception:
            pass

        return branch, commit, dirty

    def _set_relay(self, on: bool) -> None:
        """Drive relay; caller must hold self._lock."""
        self._state.heater_on = bool(on)
        GPIO.output(RELAY_GPIO, bool(on))
        if on:
            if self._state.heater_on_since is None:
                self._state.heater_on_since = time.time()
        else:
            self._state.heater_on_since = None

    def _record_event_locked(self, event_type: str, message: str, **extra) -> dict:
        event = {
            "type": event_type,
            "message": message,
            "timestamp": time.time(),
        }
        event.update(extra)
        self._state.last_event_type = event_type
        self._state.last_event_message = message
        self._state.last_event_ts = event["timestamp"]
        return event

    def _load_state_from_disk(self) -> None:
        if not os.path.isfile(self.config_path):
            return
        try:
            with open(self.config_path, "r", encoding="utf-8") as file:
                data = json.load(file)
        except Exception:
            return

        with self._lock:
            state = self._state
            state.desired_temp = float(data.get("desired_temp", state.desired_temp))
            state.limit_temp = float(data.get("limit_temp", state.limit_temp))
            state.heater_enabled = bool(data.get("heater_enabled", state.heater_enabled))
            state.use_imperial = bool(data.get("use_imperial", state.use_imperial))
            state.scheduled_start_at = data.get("scheduled_start_at")
            state.avg_heatup_rate_c_per_sec = data.get("avg_heatup_rate_c_per_sec")
            state.price_per_kwh = data.get("price_per_kwh")
            state.heater_power_kw = data.get("heater_power_kw")
            state.timer_mode = data.get("timer_mode", state.timer_mode)
            state.timer_running = bool(data.get("timer_running", state.timer_running))
            state.timer_elapsed = float(data.get("timer_elapsed", state.timer_elapsed))
            state.timer_duration = data.get("timer_duration", state.timer_duration)
            state.thermometer_mode = data.get("thermometer_mode", state.thermometer_mode)
            state.sensor_type = data.get("sensor_type", state.sensor_type)
            state.goal_sensor_type = data.get("goal_sensor_type", data.get("sensor_type", "ds18b20"))
            state.limit_sensor_type = data.get("limit_sensor_type", data.get("sensor_type", "ds18b20"))
            state.bench_sensor_id = data.get("bench_sensor_id")
            state.ceiling_sensor_id = data.get("ceiling_sensor_id")
            state.goal_spi_cs = int(data.get("goal_spi_cs", state.goal_spi_cs))
            state.limit_spi_cs = int(data.get("limit_spi_cs", state.limit_spi_cs))
            state.mqtt_enabled = bool(data.get("mqtt_enabled", False))
            state.mqtt_broker = str(data.get("mqtt_broker", ""))
            state.mqtt_port = int(data.get("mqtt_port", 1883))
            state.mqtt_username = str(data.get("mqtt_username", ""))
            state.mqtt_password = str(data.get("mqtt_password", ""))

            state.lockout_active = False
            state.lockout_reason = None
            state.confirmation_required = False
            state.confirmation_deadline = None
            state.session_started_at = None
            state.session_start_temp_c = None
            state.slow_heat_alert_sent = False
            state.last_event_type = None
            state.last_event_message = None
            state.last_event_ts = None

    def _save_state_to_disk_locked(self) -> None:
        data = {
            "desired_temp": self._state.desired_temp,
            "limit_temp": self._state.limit_temp,
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
            "thermometer_mode": self._state.thermometer_mode,
            "sensor_type": self._state.sensor_type,
            "goal_sensor_type": self._state.goal_sensor_type,
            "limit_sensor_type": self._state.limit_sensor_type,
            "bench_sensor_id": self._state.bench_sensor_id,
            "ceiling_sensor_id": self._state.ceiling_sensor_id,
            "goal_spi_cs": self._state.goal_spi_cs,
            "limit_spi_cs": self._state.limit_spi_cs,
            "mqtt_enabled": self._state.mqtt_enabled,
            "mqtt_broker": self._state.mqtt_broker,
            "mqtt_port": self._state.mqtt_port,
            "mqtt_username": self._state.mqtt_username,
            "mqtt_password": self._state.mqtt_password,
        }
        try:
            with open(self.config_path, "w", encoding="utf-8") as file:
                json.dump(data, file, indent=2)
        except Exception:
            pass

    def _run_loop(self) -> None:
        last_mqtt_publish = 0.0

        while not self._stop_event.is_set():
            events_to_publish = []
            now = time.time()

            with self._lock:
                goal_sensor_type = self._state.goal_sensor_type
                limit_sensor_type = self._state.limit_sensor_type
                thermometer_mode = self._state.thermometer_mode
                bench_sensor_id = self._state.bench_sensor_id
                ceiling_sensor_id = self._state.ceiling_sensor_id
                goal_spi_cs = self._state.goal_spi_cs
                limit_spi_cs = self._state.limit_spi_cs
                heater_on = self._state.heater_on

            goal_reading: Optional[float]
            limit_reading: Optional[float]
            sensor_fault_message = None
            mock_environment = isinstance(GPIO, MockGPIO)

            if goal_sensor_type == "ds18b20" and not W1_DEVICES and mock_environment:
                goal_reading, limit_reading = _mock_temps(heater_on)
            else:
                goal_reading = _read_sensor(
                    goal_sensor_type,
                    bench_sensor_id,
                    goal_spi_cs,
                    use_first_w1_fallback=True,
                )
                limit_reading = None
                if thermometer_mode == "dual":
                    limit_reading = _read_sensor(
                        limit_sensor_type,
                        ceiling_sensor_id,
                        limit_spi_cs,
                        use_first_w1_fallback=False,
                    )

                missing_sensors = []
                if goal_reading is None:
                    missing_sensors.append("goal")
                if thermometer_mode == "dual" and limit_reading is None:
                    missing_sensors.append("limit")

                if missing_sensors and mock_environment:
                    mock_goal, mock_limit = _mock_temps(heater_on)
                    if goal_reading is None:
                        goal_reading = mock_goal
                    if thermometer_mode == "dual" and limit_reading is None:
                        limit_reading = mock_limit
                elif missing_sensors:
                    sensor_fault_message = (
                        "Sensor read failed for "
                        + ", ".join(missing_sensors)
                        + " sensor. Session shut down for safety."
                    )

            with self._lock:
                if goal_reading is not None:
                    self._state.current_temp = goal_reading
                self._state.limit_sensor_temp = limit_reading if self._state.thermometer_mode == "dual" else None
                self._state.last_updated = now

                desired = self._state.desired_temp
                limit_cutoff = self._state.limit_temp
                enabled = self._state.heater_enabled
                session_started_at = self._state.session_started_at
                session_start_temp_c = self._state.session_start_temp_c

                if sensor_fault_message is not None:
                    if self._state.lockout_reason != "sensor_error":
                        events_to_publish.append(
                            self._record_event_locked("sensor_error", sensor_fault_message)
                        )
                    self._set_relay(False)
                    self._state.heater_enabled = False
                    self._state.lockout_active = True
                    self._state.lockout_reason = "sensor_error"
                    self._state.confirmation_required = False
                    self._state.confirmation_deadline = None
                    self._state.session_started_at = None
                    self._state.slow_heat_alert_sent = False

                # Hard lockout if either sensor exceeds absolute max.
                elif goal_reading >= MAX_TEMP_C or (
                    self._state.thermometer_mode == "dual"
                    and limit_reading is not None
                    and limit_reading >= MAX_TEMP_C
                ):
                    if self._state.lockout_reason != "max_temp":
                        events_to_publish.append(
                            self._record_event_locked(
                                "max_temp",
                                "Absolute max temperature reached. Session shut down.",
                                current_temp_c=goal_reading,
                                limit_sensor_temp_c=limit_reading,
                            )
                        )
                    self._set_relay(False)
                    self._state.heater_enabled = False
                    self._state.lockout_active = True
                    self._state.lockout_reason = "max_temp"
                    self._state.confirmation_required = False
                    self._state.confirmation_deadline = None
                    self._state.session_started_at = None
                    self._state.slow_heat_alert_sent = False
                else:
                    # Schedule logic
                    if (
                        self._state.scheduled_start_at is not None
                        and self._state.avg_heatup_rate_c_per_sec is not None
                        and not self._state.lockout_active
                    ):
                        time_until = self._state.scheduled_start_at - now
                        delta_c = max(desired - goal_reading, 0.0)
                        rate = self._state.avg_heatup_rate_c_per_sec
                        required = delta_c / rate if rate > 0 else 0.0
                        if time_until <= required:
                            self._state.heater_enabled = True
                            enabled = True
                            self._state.scheduled_start_at = None
                            if self._state.session_started_at is None:
                                self._state.session_started_at = now
                                self._state.session_start_temp_c = float(goal_reading)
                                self._state.slow_heat_alert_sent = False
                                session_started_at = now
                                session_start_temp_c = float(goal_reading)
                                events_to_publish.append(
                                    self._record_event_locked(
                                        "session_started",
                                        "Scheduled sauna session started.",
                                        session_elapsed_seconds=0,
                                        session_start_temp_c=round(float(goal_reading), 2),
                                    )
                                )

                    if enabled and session_started_at is not None:
                        session_elapsed = now - session_started_at
                        if session_elapsed >= SESSION_MAX_DURATION_SEC:
                            if self._state.lockout_reason != "session_timeout":
                                events_to_publish.append(
                                    self._record_event_locked(
                                        "session_timeout",
                                        "Sauna session reached the 4-hour safety limit and shut down.",
                                        session_elapsed_seconds=round(session_elapsed, 1),
                                    )
                                )
                            self._set_relay(False)
                            self._state.heater_enabled = False
                            self._state.lockout_active = True
                            self._state.lockout_reason = "session_timeout"
                            self._state.confirmation_required = False
                            self._state.confirmation_deadline = None
                            self._state.session_started_at = None
                            self._state.session_start_temp_c = None
                            self._state.slow_heat_alert_sent = False
                            enabled = False
                        elif (
                            not self._state.slow_heat_alert_sent
                            and session_elapsed >= SLOW_HEATUP_AFTER_SEC
                            and session_start_temp_c is not None
                            and goal_reading <= (session_start_temp_c + SLOW_HEATUP_TEMP_DELTA_C)
                        ):
                            self._state.slow_heat_alert_sent = True
                            events_to_publish.append(
                                self._record_event_locked(
                                    "heatup_slow",
                                    "Sauna heater is on but temperature has not risen enough in 15 minutes.",
                                    current_temp_c=goal_reading,
                                    setpoint_c=desired,
                                    session_start_temp_c=round(session_start_temp_c, 2),
                                    required_min_temp_c=round(
                                        session_start_temp_c + SLOW_HEATUP_TEMP_DELTA_C,
                                        2,
                                    ),
                                    session_elapsed_seconds=round(session_elapsed, 1),
                                )
                            )

                    if not self._state.lockout_active and enabled:
                        if self._state.thermometer_mode == "single":
                            if goal_reading < desired - HYSTERESIS:
                                self._set_relay(True)
                            elif goal_reading >= desired:
                                self._set_relay(False)
                        else:
                            goal_needs_heat = goal_reading < desired - HYSTERESIS
                            goal_satisfied = goal_reading >= desired
                            limit_safe = (
                                limit_reading is None
                                or limit_reading < limit_cutoff - HYSTERESIS
                            )
                            limit_reached = (
                                limit_reading is not None
                                and limit_reading >= limit_cutoff - HYSTERESIS
                            )

                            if goal_needs_heat and limit_safe:
                                self._set_relay(True)
                            elif goal_satisfied or limit_reached:
                                self._set_relay(False)
                    elif self._state.heater_on:
                        self._set_relay(False)

                if self._state.heater_on_since is not None and goal_reading >= desired:
                    elapsed = now - self._state.heater_on_since
                    if elapsed > 0:
                        if self._state.time_to_setpoint is None:
                            self._state.time_to_setpoint = elapsed
                        delta_c = max(desired - (goal_reading - 5.0), 0.0)
                        if delta_c > 0:
                            new_rate = delta_c / elapsed
                            old_rate = self._state.avg_heatup_rate_c_per_sec
                            if old_rate is None:
                                self._state.avg_heatup_rate_c_per_sec = new_rate
                            else:
                                self._state.avg_heatup_rate_c_per_sec = 0.3 * new_rate + 0.7 * old_rate

                if self._state.timer_running and self._state.timer_start_ts is not None:
                    self._state.timer_elapsed += CONTROL_INTERVAL_SEC

            for event in events_to_publish:
                self._mqtt.publish_event(event)

            if now - last_mqtt_publish >= MQTT_PUBLISH_INTERVAL_SEC:
                self._mqtt.publish_state(self.get_state_snapshot())
                last_mqtt_publish = now

            time.sleep(CONTROL_INTERVAL_SEC)


if __name__ == "__main__":
    controller = SaunaController()
    try:
        while True:
            print(controller.get_state_snapshot())
            time.sleep(5)
    except KeyboardInterrupt:
        controller.stop()
