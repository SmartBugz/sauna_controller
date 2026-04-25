# Smart Sauna Controller

Python/Flask-based smart sauna controller for Raspberry Pi 4 with a web UI, dual-sensor safety mode, and Home Assistant MQTT integration.

## Features

### Core Control

- Bang-bang thermostat with configurable ±2.5°C hysteresis.
- 4-hour maximum session runtime starting from the user turning heating on.
- Hard overtemperature lockout at 105°C to prevent runaway heating.
- Active-HIGH relay control on GPIO 17.

### Sensor Support

- Single-sensor mode for standard thermostat control.
- Dual-sensor mode for goal sensor plus independent limit-sensor safety.
- Goal temperature controls heating.
- Limit temperature prevents ceiling or heater-area overheating.
- Supported sensor types:
  - DS18B20 1-Wire probes on GPIO 4.
  - MAX31855 SPI thermocouples with configurable chip-select pins.

### Integration

- Home Assistant MQTT auto-discovery.
- Configurable MQTT broker hostname, port, and credentials.
- Runtime publishing of temperature and heater state.
- MQTT-discovered safety and session entities for Home Assistant dashboards and automations.
- MQTT event topic for Home Assistant notifications and safety automations.
- Home Assistant can:
  - Read current sauna temperature.
  - Change the goal temperature setpoint.
  - Turn heating on or off.

### User Interface

- Web dashboard for heater status, temperature, timer, schedule, and cost tracking.
- Kiosk UI optimized for the Raspberry Pi 7-inch touchscreen.
- Live runtime configuration for thermometer and Home Assistant settings.
- Persistent configuration stored in `sauna_state.json`.

## Project Structure

- `main.py`: Flask web application entry point.
- `sauna_controller.py`: Control loop, sensor reading, relay control, persistence, and MQTT publishing.
- `templates/index.html`: Main web UI template.
- `static/style.css`: Application styling.
- `requirements.txt`: Python dependencies.

## Quick Start on a PC

The app runs in mock mode on non-Pi systems. That makes it useful for testing the UI and configuration workflow before deploying to hardware.

```bash
cd sauna_controller
python -m venv .venv
```

### Windows

```powershell
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python .\main.py
```

### macOS or Linux

```bash
source .venv/bin/activate
pip install -r requirements.txt
python ./main.py
```

Open <http://localhost:5000> in your browser.

### Mock Mode Notes

- Mock temperatures rise and fall based on heater state.
- Mock GPIO logs relay activity instead of switching hardware.
- MQTT can still be tested if you point the app at a reachable broker.
- The full configuration UI is available in mock mode.

## Raspberry Pi Deployment

See [PI_SETUP.md](PI_SETUP.md) for the full Raspberry Pi wiring, service, kiosk, and MQTT setup.

That guide covers:

- DS18B20 and MAX31855 wiring.
- Running the Flask server on the Pi.
- Automatic backend startup with `systemd`.
- Automatic kiosk startup with Chromium.
- Home Assistant MQTT setup and validation.

### Pi Run and Auto-Start Summary

- Run the server manually with `/home/pi/sauna_controller/run_sauna.sh`.
- Open the UI from another device at `http://<pi-ip>:5000`.
- The kiosk screen points at `http://localhost:5000`.
- Backend auto-start uses `sauna.service`.
- Kiosk auto-start uses LXDE autostart plus Chromium kiosk mode.

### Pi Desktop Shortcut Installer

If you want one-command setup for desktop launchers on the Pi home screen:

```bash
cd /home/pi/sauna_controller
chmod +x install_pi_shortcuts.sh launch_sauna_kiosk.sh launch_sauna_service_kiosk.sh exit_sauna_kiosk.sh run_sauna.sh
./install_pi_shortcuts.sh
```

This creates:

- `~/Desktop/Smart Sauna Server.desktop`
- `~/Desktop/Smart Sauna Kiosk.desktop`
- `~/Desktop/Smart Sauna Start Kiosk.desktop` (restarts `sauna.service` and opens kiosk)
- `~/Desktop/Smart Sauna Exit Kiosk.desktop` (closes Chromium kiosk quickly)

For true one-touch service restart from desktop, allow passwordless restart for the service:

```bash
echo "$(whoami) ALL=(root) NOPASSWD: /bin/systemctl restart sauna.service" | sudo tee /etc/sudoers.d/sauna-restart
sudo chmod 440 /etc/sudoers.d/sauna-restart
```

To also auto-open kiosk mode after desktop login:

```bash
./install_pi_shortcuts.sh --autostart
```

### Reliable Update Behavior on Pi

This project is configured so runtime artifacts do not block Git updates on the device:

- `sauna_state.json` is local runtime state and is not tracked by Git.
- `__pycache__/` and `.pyc` files are ignored.
- `.sh` scripts are forced to LF line endings for Raspberry Pi compatibility.
- `run_sauna.sh` preserves `sauna_state.json` across pull attempts and removes stale pycache artifacts before pulling.

For existing Pi clones that predate this change, run this one-time cleanup:

```bash
cd /home/pi/sauna_controller
git rm --cached -r __pycache__ sauna_state.json
cp sauna_state.example.json sauna_state.json 2>/dev/null || true
git status
```

## Configuration

### Thermometer Setup

UI path: `Settings` tab -> `Edit Thermometer Setup`

#### Single-Sensor Mode

- Set `Mode` to `Single`.
- Set `Goal Sensor` to `DS18B20` or `Thermocouple`.
- For DS18B20, enter the `Bench Sensor ID`.
- For thermocouple, set the `Goal SPI CS` pin.

#### Dual-Sensor Mode

- Set `Mode` to `Dual`.
- Set `Goal Sensor` and `Limit Sensor` independently.
- For DS18B20, enter both `Bench Sensor ID` and `Ceiling Sensor ID`.
- For thermocouples, set both `Goal SPI CS` and `Limit SPI CS`.
- Set `Limit Temp` to the safety cutoff temperature.

Click `Save` in the dialog to persist the configuration.

### Limit Temperature

The limit temperature is used in dual-sensor mode to shut heating down before the ceiling or heater area overheats.

Example:

- Goal temperature: 80°C.
- Limit temperature: 95°C.

### Home Assistant MQTT Setup

UI path: `Settings` tab -> `Edit Home Assistant Setup`

To enable Home Assistant integration:

1. Make sure you have a working MQTT broker.
2. Open the `Settings` tab.
3. Click `Edit Home Assistant Setup`.
4. Check `Enable MQTT`.
5. Enter broker hostname or IP, port, username, and password.
6. Click `Save`.
7. Confirm the status changes to `Connected`.

### Home Assistant Capabilities

Once connected, Home Assistant can:

- Read current sauna temperature.
- Change the goal temperature setpoint.
- Turn heating mode on or off.
- Show whether the heater relay is energized.
- Show whether a safety lockout is active and why.
- Track session elapsed and remaining safety runtime.
- Trigger automations from sauna safety events.

The controller also exposes limit-sensor telemetry, relay state, safety lockout state, lockout reason, session timers, and last-event status as MQTT-discovered entities.

### MQTT Topics

Discovery topics:

- `homeassistant/climate/sauna_controller/config`
- `homeassistant/sensor/sauna_controller_limit/config`
- `homeassistant/binary_sensor/sauna_controller_heater_relay/config`
- `homeassistant/binary_sensor/sauna_controller_safety_lockout/config`
- `homeassistant/sensor/sauna_controller_lockout_reason/config`
- `homeassistant/sensor/sauna_controller_session_elapsed/config`
- `homeassistant/sensor/sauna_controller_session_remaining/config`
- `homeassistant/sensor/sauna_controller_last_event/config`
- `homeassistant/sensor/sauna_controller_last_event_message/config`

Runtime topics:

- State: `sauna_controller/state`
- Availability: `sauna_controller/availability`
- Events: `sauna_controller/event`

Command topics:

- `sauna_controller/cmd/mode`
- `sauna_controller/cmd/setpoint`
- `sauna_controller/cmd/limit_temp`

Published state payload includes:

- `current_temp_c`
- `setpoint_c`
- `mode`
- `action`
- `limit_sensor_temp_c`
- `limit_temp_c`
- `thermometer_mode`
- `heater_on`
- `lockout_active`
- `lockout_reason`
- `session_elapsed_seconds`
- `session_remaining_seconds`
- `last_event_type`
- `last_event_message`

Event payloads on `sauna_controller/event` include:

- `session_started`
- `session_stopped`
- `session_timeout`
- `max_temp`
- `heatup_slow`
- `sensor_error`

`heatup_slow` triggers when heating has been enabled for 15 minutes and temperature has not increased by at least 5°C from session start.

### Home Assistant Notification Automations

This repo includes ready-to-use Home Assistant automation YAML for sauna event notifications:

- `home_assistant/sauna_notifications_automations.yaml`

The file listens to `sauna_controller/event` and triggers notifications for:

- `session_timeout`
- `max_temp`
- `sensor_error`
- `heatup_slow`

By default, it uses `notify.notify` plus persistent notifications. In Home Assistant, replace `notify.notify` with your preferred notifier (for example a mobile app notification service) if desired.

## Notes

- All configuration is persisted to `sauna_state.json` in the project root.
- Older single-sensor configurations still load and run.
- For production Pi deployments, use [PI_SETUP.md](PI_SETUP.md) for the full auto-start and kiosk instructions.
