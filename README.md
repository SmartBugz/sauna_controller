# Smart Sauna Controller

Python/Flask-based smart sauna controller for Raspberry Pi 4 with a web UI, dual-sensor safety mode, and Home Assistant MQTT integration.

## Features

### Core Control

- Bang-bang thermostat with configurable ±2.5°C hysteresis.
- 2-hour max runtime with confirmation window as a safety cutoff.
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
chmod +x install_pi_shortcuts.sh launch_sauna_kiosk.sh run_sauna.sh
./install_pi_shortcuts.sh
```

This creates:

- `~/Desktop/Smart Sauna Server.desktop`
- `~/Desktop/Smart Sauna Kiosk.desktop`

To also auto-open kiosk mode after desktop login:

```bash
./install_pi_shortcuts.sh --autostart
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

The controller also exposes limit-sensor telemetry as a separate MQTT-discovered sensor.

### MQTT Topics

Discovery topics:

- `homeassistant/climate/sauna_controller/config`
- `homeassistant/sensor/sauna_controller_limit/config`

Runtime topics:

- State: `sauna_controller/state`
- Availability: `sauna_controller/availability`

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

## Notes

- All configuration is persisted to `sauna_state.json` in the project root.
- Older single-sensor configurations still load and run.
- For production Pi deployments, use [PI_SETUP.md](PI_SETUP.md) for the full auto-start and kiosk instructions.
