# Raspberry Pi Setup for Smart Sauna Controller

This guide covers:

- DS18B20 and MAX31855 wiring.
- Running the Flask server manually.
- Starting the backend automatically with `systemd`.
- Starting Chromium automatically in kiosk mode.
- Connecting Home Assistant over MQTT.
- Making the whole stack start automatically after boot.

> Paths in this guide assume the project lives at `/home/pi/sauna_controller` and the virtual environment is at `/home/pi/sauna_controller/.venv`.
>
> The app starts in single-sensor DS18B20 mode by default. Use `Settings` -> `Edit Thermometer Setup` in the UI to change sensor mode or sensor type.

---

## 1. Hardware Wiring

Choose one sensor configuration.

### Option A: Dual DS18B20 Sensors

Use two DS18B20 probes in dual-sensor mode:

- Bench sensor: primary control sensor at sitting height.
- Ceiling sensor: safety cutoff sensor near the heater or ceiling.

Wire both sensors to the same 1-Wire bus:

- Data -> GPIO 4.
- VCC -> 3.3V.
- GND -> Ground.
- Pull-up resistor: 4.7kΩ between GPIO 4 data and 3.3V.

Connection diagram:

```text
Pi GPIO 4  ────────┬─────────── DS18B20_1 (Bench)
                   │
                   ├─────────── DS18B20_2 (Ceiling)
                   │
                  [R=4.7k]
                   │
                Pi 3.3V
```

Dual-mode control logic:

- Heater turns on when `bench_temp < target - hysteresis` and `ceiling_temp < limit_temp - hysteresis`.
- Heater turns off when `bench_temp > target + hysteresis` or `ceiling_temp >= limit_temp - hysteresis`.
- Hard lockout occurs if either sensor reaches 105°C.
- Heating also shuts down automatically 4 hours after the session is turned on.

Find DS18B20 sensor IDs:

```bash
ls -la /sys/bus/w1/devices/ | grep 28-
```

Use those IDs in `Settings` -> `Edit Thermometer Setup`.

### Option B: Single DS18B20 Sensor

Wire one DS18B20 to GPIO 4 using the same wiring as above.

To upgrade later to dual-sensor mode:

1. Connect the second DS18B20 to the same GPIO 4 bus.
2. Find both sensor IDs.
3. Open `Settings` -> `Edit Thermometer Setup`.
4. Change `Mode` to `Dual`.
5. Enter both sensor IDs and save.

### Option C: MAX31855 Thermocouples

Use MAX31855 boards if you prefer thermocouples instead of DS18B20 probes.

Required hardware:

- MAX31855 breakout board.
- K-type thermocouple probe.
- SPI pins on the Pi.
- One CS pin per MAX31855 board.

Default pin usage:

- SCLK -> GPIO 11.
- MOSI -> GPIO 10.
- MISO -> GPIO 9.
- Goal thermocouple CS -> GPIO 8.
- Limit thermocouple CS -> GPIO 7.

Single thermocouple wiring:

```text
MAX31855_1          Pi GPIO
─────────
VCC      ─────->  3.3V
GND      ─────->  GND
CLK      ─────->  GPIO 11
MOSI     ─────->  GPIO 10
MISO     ─────->  GPIO 9
CS       ─────->  GPIO 8
TC+/TC-  ─────->  Thermocouple leads
```

Dual thermocouple wiring:

```text
Goal MAX31855       Pi GPIO
CS       ─────->  GPIO 8
CLK      ─────->  GPIO 11
MOSI     ─────->  GPIO 10
MISO     ─────->  GPIO 9

Limit MAX31855      Pi GPIO
CS       ─────->  GPIO 7
CLK      ─────->  GPIO 11
MOSI     ─────->  GPIO 10
MISO     ─────->  GPIO 9
```

Install the Python dependency:

```bash
source /home/pi/sauna_controller/.venv/bin/activate
pip install adafruit-circuitpython-max31855
```

Then configure the UI:

1. Open `Settings` -> `Edit Thermometer Setup`.
2. Set `Goal Sensor` to `Thermocouple`.
3. If using dual mode, set `Limit Sensor` to `Thermocouple`.
4. Set the correct `Goal SPI CS` and `Limit SPI CS` values.
5. Click `Save`.

### Relay Wiring

The controller uses GPIO 17 for the heater relay.

- Relay control pin: GPIO 17.
- Relay type: active-HIGH.
- HIGH means heater on, LOW means heater off.

Example relay wiring:

```text
Pi GPIO 17 ─────> Relay Module Input
Relay COM  ─────> Heater Contactor Coil (-)
Relay NO   ─────> Heater Contactor Coil (+)
```

---

## 2. Enable Required Pi Interfaces

### Enable 1-Wire for DS18B20

If you are using DS18B20 sensors:

```bash
sudo raspi-config
```

Navigate to:

- `Interface Options` -> `1-Wire` -> `Yes`

Reboot:

```bash
sudo reboot
```

Verify detection:

```bash
ls -la /sys/bus/w1/devices/ | grep 28-
```

### Enable SPI for MAX31855

If you are using thermocouples:

```bash
sudo raspi-config
```

Navigate to:

- `Interface Options` -> `SPI` -> `Yes`

Reboot and verify:

```bash
ls /dev/spidev*
```

---

## 3. Run the Server Manually

Create a helper script:

```bash
cd /home/pi/sauna_controller
cat > run_sauna.sh << 'EOF'
#!/bin/bash
cd /home/pi/sauna_controller
source .venv/bin/activate
exec python main.py
EOF

chmod +x /home/pi/sauna_controller/run_sauna.sh
```

Run the server manually:

```bash
cd /home/pi/sauna_controller
./run_sauna.sh
```

Open the UI:

- From the Pi: <http://localhost:5000>
- From another device: `http://<pi-ip>:5000`

### One-Time Git Migration for Older Clones

If your Pi clone is older, do this once so runtime files never block pulls again:

```bash
cd /home/pi/sauna_controller
git rm --cached -r __pycache__ sauna_state.json
cp sauna_state.example.json sauna_state.json 2>/dev/null || true
git add .gitignore .gitattributes sauna_state.example.json run_sauna.sh
git commit -m "Ignore runtime artifacts and harden Pi startup pulls"
```

After this, `sauna_state.json` remains local on-device state and future `git pull --ff-only` runs are much less likely to fail.

---

## 4. Start the Backend Automatically on Boot

Create the service file:

```bash
sudo nano /etc/systemd/system/sauna.service
```

Paste:

```ini
[Unit]
Description=Smart Sauna Controller
After=network.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/sauna_controller
ExecStart=/home/pi/sauna_controller/run_sauna.sh
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

Enable and start it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable sauna.service
sudo systemctl start sauna.service
sudo systemctl status sauna.service
```

Useful commands:

```bash
journalctl -u sauna.service -f
sudo systemctl stop sauna.service
sudo systemctl disable sauna.service
```

---

## 5. Configure Thermometer Setup in the UI

On first boot, the app uses single-sensor DS18B20 mode by default.

To configure sensors:

1. Open `http://<pi-ip>:5000`.
2. Go to the `Settings` tab.
3. Click `Edit Thermometer Setup`.
4. Set `Mode`, sensor types, sensor IDs, and SPI CS pins as needed.
5. Click `Save`.
6. Refresh the page to confirm the readings and mode.

Configuration persists in `sauna_state.json`.

---

## 6. Configure Home Assistant MQTT

Prerequisites:

- A reachable MQTT broker.
- Network connectivity between the Pi and Home Assistant.

Setup steps:

1. Open `http://<pi-ip>:5000`.
2. Go to the `Settings` tab.
3. Click `Edit Home Assistant Setup`.
4. Enable MQTT.
5. Enter broker host, port, username, and password.
6. Click `Save`.
7. Confirm the status changes to `Connected`.

### What Home Assistant Can Do

Once connected, Home Assistant can:

- Read current sauna temperature.
- Change the goal temperature setpoint.
- Turn heating on and off.
- Show whether the heater relay is on.
- Show whether a safety lockout is active and why.
- Track session elapsed and remaining time.
- Trigger automations from sauna event messages.

The app also publishes limit-sensor temperature, relay state, safety lockout state, lockout reason, session timers, and last-event details as separate MQTT-discovered entities.

### Discovery and Runtime Topics

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

### Validate from Home Assistant

1. Open the discovered sauna climate entity in Home Assistant.
2. Confirm current temperature updates as the sauna temperature changes.
3. Change target temperature in Home Assistant and verify the Pi UI updates.
4. Set HVAC mode to `heat` and `off` and verify the sauna follows.
5. Build automations from `sauna_controller/event` for `session_timeout`, `max_temp`, `heatup_slow`, and `sensor_error` notifications.

`heatup_slow` is emitted when heating has been enabled for 15 minutes and temperature has not increased by at least 5°C from session start.

### Enable Home Assistant Notifications (Recommended)

Use the included automation file from this repo:

- `home_assistant/sauna_notifications_automations.yaml`

Suggested setup:

1. Copy the file into your Home Assistant config, for example: `/config/automations/sauna_notifications_automations.yaml`.
2. In Home Assistant `automations.yaml`, include it:

```yaml
automation: !include automations/sauna_notifications_automations.yaml
```

1. Restart Home Assistant.
2. Trigger a test event from the sauna app (for example by waiting for `heatup_slow` or forcing a safe shutdown test) and confirm notification delivery.

Notes:

- The file uses `notify.notify` by default.
- Replace that service with your preferred notifier (for example your mobile app notify service) for push notifications.

### Manual MQTT Commands

```bash
# Enable heater
mosquitto_pub -h <broker-ip> -t sauna_controller/cmd/mode -m "heat"

# Disable heater
mosquitto_pub -h <broker-ip> -t sauna_controller/cmd/mode -m "off"

# Set goal temp to 85 C
mosquitto_pub -h <broker-ip> -t sauna_controller/cmd/setpoint -m "85"

# Set limit temp to 95 C
mosquitto_pub -h <broker-ip> -t sauna_controller/cmd/limit_temp -m "95"
```

---

## 7. Launch Chromium in Kiosk Mode Automatically

Enable desktop auto-login first:

```bash
sudo raspi-config
```

Navigate to:

- `System Options` -> `Boot / Auto Login` -> `Desktop Autologin`

Create or edit the LXDE autostart file:

```bash
mkdir -p /home/pi/.config/lxsession/LXDE-pi
nano /home/pi/.config/lxsession/LXDE-pi/autostart
```

Add:

```text
@xset s off
@xset -dpms
@xset s noblank

@chromium-browser --kiosk --incognito http://localhost:5000
```

This gives you fully automatic startup:

- `sauna.service` starts the backend on boot.
- Desktop autologin starts the Pi GUI session.
- LXDE autostart launches Chromium at `http://localhost:5000`.

Reboot to test:

```bash
sudo reboot
```

---

## 8. Install Desktop Shortcuts (Recommended)

Use the included installer to create home-screen launchers:

```bash
cd /home/pi/sauna_controller
chmod +x install_pi_shortcuts.sh launch_sauna_kiosk.sh launch_sauna_service_kiosk.sh run_sauna.sh
./install_pi_shortcuts.sh
```

This creates two desktop shortcuts:

- `~/Desktop/Smart Sauna Server.desktop` to start the backend in a terminal.
- `~/Desktop/Smart Sauna Kiosk.desktop` to launch Chromium in kiosk mode and auto-start backend if needed.
- `~/Desktop/Smart Sauna Start Kiosk.desktop` to restart `sauna.service` and then open Chromium kiosk mode.

For true one-touch restart without password prompts, allow passwordless restart for this service only:

```bash
echo "$(whoami) ALL=(root) NOPASSWD: /bin/systemctl restart sauna.service" | sudo tee /etc/sudoers.d/sauna-restart
sudo chmod 440 /etc/sudoers.d/sauna-restart
```

If you also want kiosk mode to auto-open on each desktop login:

```bash
./install_pi_shortcuts.sh --autostart
```

This adds an autostart entry at:

- `~/.config/autostart/smart-sauna-kiosk.desktop`

---

## 9. Remote Access

When the backend is running, devices on your network can open:

```text
http://<pi-ip-address>:5000
```

Find the Pi IP:

```bash
hostname -I
```

---

## 10. Configuration File Reference

All settings are saved to `sauna_state.json`.

Single-sensor DS18B20 example:

```json
{
  "desired_temp": 80,
  "current_temp": 45,
  "heater_enabled": false,
  "thermometer_mode": "single",
  "goal_sensor_type": "ds18b20",
  "limit_sensor_type": "ds18b20",
  "bench_sensor_id": "28-0119a94519ee",
  "ceiling_sensor_id": null,
  "limit_temp": 85,
  "goal_spi_cs": 8,
  "limit_spi_cs": 7,
  "use_imperial": false,
  "mqtt_enabled": false,
  "mqtt_broker": "localhost",
  "mqtt_port": 1883,
  "mqtt_username": "",
  "mqtt_password": ""
}
```

Dual-sensor DS18B20 example:

```json
{
  "desired_temp": 80,
  "current_temp": 75,
  "thermometer_mode": "dual",
  "goal_sensor_type": "ds18b20",
  "limit_sensor_type": "ds18b20",
  "bench_sensor_id": "28-0119a94519ee",
  "ceiling_sensor_id": "28-01140c63a7ee",
  "limit_temp": 95,
  "mqtt_enabled": true,
  "mqtt_broker": "192.168.1.100",
  "mqtt_port": 1883,
  "mqtt_username": "pi",
  "mqtt_password": "secure_password"
}
```

Dual thermocouple example:

```json
{
  "thermometer_mode": "dual",
  "goal_sensor_type": "thermocouple",
  "limit_sensor_type": "thermocouple",
  "goal_spi_cs": 8,
  "limit_spi_cs": 7,
  "limit_temp": 100
}
```

---

## 11. Troubleshooting

### No Temperature Reading

If using DS18B20:

```bash
ls -la /sys/bus/w1/devices/ | grep 28-
```

- If no `28-*` devices appear, 1-Wire is not enabled or wiring is wrong.
- If sensors appear but the UI shows `N/A`, check the configured sensor IDs.

If using MAX31855:

```bash
ls /dev/spidev*
```

- If no SPI devices appear, enable SPI in `raspi-config`.
- Recheck SCLK, MOSI, MISO, CS, power, and ground wiring.

### Heater Will Not Turn On

- Verify GPIO 17 is wired to the relay input.
- Check for a lockout condition in the UI.
- Review logs with `journalctl -u sauna.service -f`.

### MQTT Will Not Connect

- Verify broker host, port, username, and password.
- Test broker reachability from the Pi.
- Review logs with `journalctl -u sauna.service | grep -i mqtt`.
- Check that the UI status changes to `Connected`.

### Kiosk Does Not Appear

- Verify `sauna.service` is running.
- Verify Chromium is installed.
- Confirm desktop autologin is enabled.
- Confirm the LXDE autostart file contains the Chromium kiosk command.

### Configuration Does Not Persist

- Verify `/home/pi/sauna_controller/sauna_state.json` is writable by user `pi`.
- Check free disk space.

### Dual-Sensor Mode Does Not Engage

- Confirm `Mode` is set to `Dual` in `Edit Thermometer Setup`.
- Verify both sensor IDs or both thermocouple CS pins are configured.
- Save and refresh the UI.
- Check SPI wiring—especially CS pin (should be clean, not floating)
- If only one thermocouple works in dual mode, check CS pin for that board

---

## Next Steps

Once the Pi is running in kiosk mode:

1. **Test basic control**: Set a goal temperature 5°C below current, verify heater turns on
2. **Verify dual-sensor (if configured)**: Manually warm ceiling sensor, verify heater shuts off when hitting limit
3. **Test MQTT (optional)**: If using HA, check climate entity appears; set temp from HA and verify Pi responds
4. **Monitor logs**: Keep `journalctl -u sauna.service -f` open to catch any runtime errors
5. **Schedule maintenance**: Plan monthly checks of sensor connections and relay mechanical operation

For issues or customization, check the [README.md](README.md) for additional context on control logic and configuration options.
