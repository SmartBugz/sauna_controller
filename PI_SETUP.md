# Raspberry Pi Setup for Smart Sauna Controller

This guide covers:

- **Hardware setup**: DS18B20 dual-sensor 1-Wire wiring OR MAX31855 thermocouple SPI wiring (choose one)
- **Service auto-start**: systemd service for automatic startup on boot/reboot
- **Kiosk mode**: Auto-launch Chromium browser in full-screen on the 7" touchscreen
- **MQTT integration**: Connect to Home Assistant via MQTT auto-discovery
- **Configuration**: Set up thermometer mode and sensor selection
- **Troubleshooting**: Common issues and fixes

> All paths assume: project cloned to `/home/pi/sauna_controller` and virtualenv at `/home/pi/sauna_controller/.venv`
>
> The app will run in **single-sensor DS18B20 mode by default**. Use the web UI ("Thermometer Setup" widget) to switch modes or sensor types at runtime.

---

## 0. Hardware Wiring

Choose **one** of the following sensor configurations:

### Option A: Dual DS18B20 Sensors (Recommended for reliability)

The system can use **two** DS18B20 1-Wire temperature sensors in dual-sensor mode:
- **Sensor 1 (Bench Level)**: Primary control sensor. Place at sitting height where users feel the heat.
- **Sensor 2 (Ceiling Level)**: Safety cutoff sensor. Place near ceiling above the heater.

**Wiring (both sensors share the same GPIO 4 data line):**
- **Data pin** → GPIO 4 (both sensors in parallel)
- **VCC** → 3.3V (both sensors in parallel)
- **GND** → Ground (both sensors in parallel)
- **Pull-up resistor:** 4.7kΩ between data line and 3.3V (shared, required for 1-Wire protocol)

**Connection diagram:**
```
Pi GPIO 4  ────────┬─────────── DS18B20_1 (Bench)
                   │
                   ├─────────── DS18B20_2 (Ceiling)
                   │
                  [R=4.7k]
                   │
                Pi 3.3V
```

**Control Logic (Dual-Sensor Mode):**
- Heater turns ON when: `bench_temp < target - hysteresis` **AND** `ceiling_temp < limit_temp - hysteresis`
- Heater turns OFF when: `bench_temp > target + hysteresis` **OR** `ceiling_temp >= limit_temp - hysteresis`
- Hard lockout if either sensor ≥ 105°C

This prevents the ceiling from overheating while ensuring adequate heat at bench level.

**To find sensor IDs after wiring:**
```bash
ls -la /sys/bus/w1/devices/ | grep 28-
```

You'll see entries like: `28-0119a94519ee` and `28-01140c63a7ee`

Use these IDs in the web UI's "Thermometer Setup" widget.

---

### Option B: Single DS18B20 Sensor (Budget option)

Wire only **one** sensor to GPIO 4 (same wiring as above, just omit the second sensor). The app will run in single-sensor mode by default. Upgrade to dual-sensor by:
1. Connect the second sensor to GPIO 4
2. Find its ID with `ls -la /sys/bus/w1/devices/ | grep 28-`
3. In the web UI, switch "Thermometer Setup" from `Single` to `Dual` and enter both sensor IDs

---

### Option C: MAX31855 Thermocouple on SPI (High-temperature option)

For temperatures above typical sauna range (~105°C max), use a K-type thermocouple with MAX31855 ADC on SPI.

**Required hardware:**
- MAX31855 breakout board (Adafruit or compatible)
- K-type thermocouple probe
- 3× Pi SPI pins (CLK, MOSI, MISO on GPIO 10, 9, 11)
- 2× Pi GPIO pins for slave select (CS) – defaults are GPIO 8 (goal) and GPIO 7 (limit)

**Wiring (per Adafruit MAX31855 documentation):**

For one thermocouple (single-sensor mode):
```
MAX31855_1          Pi GPIO
─────────
  VCC     ─────->  3.3V
  GND     ─────->  GND
  CLK     ─────->  GPIO 11 (SCLK)
  MOSI    ─────->  GPIO 10 (MOSI)
  MISO    ─────->  GPIO 9 (MISO)
  CS      ─────->  GPIO 8 (configurable)
  TC+/TC- ─────->  K-type thermocouple leads
```

For dual thermocouples (dual-sensor mode):
```
Goal MAX31855      Pi GPIO
  CS  ─────->  GPIO 8 (configurable, default goal CS)
  CLK ─────->  GPIO 11 (shared SPI)
  MOSI─────->  GPIO 10 (shared SPI)
  MISO─────->  GPIO 9 (shared SPI)

Limit MAX31855     Pi GPIO
  CS  ─────->  GPIO 7 (configurable, default limit CS)
  CLK ─────->  GPIO 11 (shared SPI)
  MOSI─────->  GPIO 10 (shared SPI)
  MISO─────->  GPIO 9 (shared SPI)

Both boards:
  VCC ─────->  3.3V (shared)
  GND ─────->  GND (shared)
```

**Installation:**
On the Pi, install the Adafruit library:
```bash
source /home/pi/sauna_controller/.venv/bin/activate
pip install adafruit-circuitpython-max31855
```

**Configuration:**
In the web UI "Thermometer Setup" widget:
1. Change "Sensor Type" to `Thermocouple (MAX31855)`
2. Set "Goal SPI CS" to GPIO 8 (or your chosen pin)
3. Set "Limit SPI CS" to GPIO 7 (or your chosen pin, only needed in dual mode)

---

### Relay Control

All modes use GPIO 17 for heater relay control:
- **Relay control pin:** GPIO 17 (BCM numbering)
- **Relay type:** Active-HIGH (HIGH = heater ON, LOW = heater OFF)
- Connect relay module input to GPIO 17, output contacts to sauna heater contactor

**Wiring example:**
```
Pi GPIO 17 ─────> Relay Module Input
Relay COM  ─────> Sauna Heater Contactor Coil (-)
Relay NO   ─────> Sauna Heater Contactor Coil (+)
           (or NC depending on heater design)
```

---

## 1. Prepare 1-Wire (GPIO 4) on Pi OS

If using DS18B20 sensors, enable 1-Wire:

```bash
sudo raspi-config
```

Navigate to:
- `Interface Options` → `I2C` → `Yes` (enable I2C if not already)
- `Interface Options` → `1-Wire` → `Yes`

Reboot:
```bash
sudo reboot
```

After reboot, verify sensors are detected:
```bash
ls -la /sys/bus/w1/devices/ | grep 28-
```

You should see one or more `28-*` entries (one per DS18B20).

---

## 2. Create a run script for the app

Create a helper script to activate the virtualenv and start `main.py`.

Create a small helper script to activate the virtualenv and start `main.py`.

From the Pi:

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

This script will be used by the systemd service and any manual shortcuts.

---

## 3. Auto-start the backend with systemd

Create a systemd service so the app starts automatically on boot and restarts after crashes.

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

Enable and start the service:

```bash
sudo systemctl daemon-reload
sudo systemctl enable sauna.service
sudo systemctl start sauna.service
sudo systemctl status sauna.service
```

Check logs:
```bash
journalctl -u sauna.service -f
```

To stop (e.g., for debugging):
```bash
sudo systemctl stop sauna.service
```

To disable on boot:
```bash
sudo systemctl disable sauna.service
```

---

## 4. Configure Thermometer Mode and Sensors

On first boot, the app runs in **single-sensor DS18B20 mode by default**. To switch modes or sensor types:

1. Open the web UI: `http://<pi-ip>:5000`
2. Scroll down to "Thermometer Setup" widget
3. Select desired configuration:
   - **Mode**: `Single` or `Dual`
   - **Sensor Type**: `DS18B20 (1-Wire)` or `Thermocouple (MAX31855)`
   - **Sensor IDs / CS Pins**: Enter appropriate identifiers
4. Click "Save Thermometer Setup"
5. Refresh the page to confirm changes took effect

**Configuration persists** in `sauna_state.json`; you can edit the file directly if needed.

---

## 5. Optional: Home Assistant MQTT Integration

To integrate with Home Assistant via MQTT:

**Prerequisites:**
- Home Assistant with MQTT broker running (e.g., Mosquitto add-on)
- Network connectivity between Pi and HA instance

**Setup:**

1. Obtain your broker's hostname/IP and port (default: `1883`, or `8883` for SSL)
2. If using authentication, have username and password ready
3. In the kiosk UI, scroll to "Home Assistant MQTT" widget
4. Check "Enable MQTT integration"
5. Enter:
   - **Broker Host/IP**: (e.g., `192.168.1.100` or `hassio.local`)
   - **Broker Port**: (e.g., `1883`)
   - **Username**: (if your broker requires auth)
   - **Password**: (if your broker requires auth)
6. Click "Save MQTT Settings"
7. Status should show "Connected" within a few seconds

**Home Assistant Discovery:**

Once connected, Home Assistant will auto-discover:
- **Climate entity** (`climate.sauna_controller_climate`):
  - Shows current goal temperature and ceiling limit
  - Allows setting goal temperature externally
  - Displays current bench/ceiling temps
- **Limit sensor** (`sensor.sauna_controller_limit_temp`):
  - Read-only; displays ceiling limit temperature
  - Updates every 5 seconds

**Manual MQTT Commands:**

You can also send commands via MQTT client:
```bash
# Enable heater
mosquitto_pub -h <broker-ip> -t sauna_controller/cmd/mode -m "heat"

# Disable heater
mosquitto_pub -h <broker-ip> -t sauna_controller/cmd/mode -m "off"

# Set goal temp to 85°C
mosquitto_pub -h <broker-ip> -t sauna_controller/cmd/setpoint -m "85"

# Set limit temp to 95°C (dual mode only)
mosquitto_pub -h <broker-ip> -t sauna_controller/cmd/limit_temp -m "95"
```

**Troubleshooting MQTT:**
- Check "Connected" status in UI; if shows "Disconnected", verify broker IP/port/credentials
- Check systemd logs: `journalctl -u sauna.service | grep -i mqtt`
- Verify Home Assistant MQTT broker is running
- Test connectivity from Pi: `mosquitto_pub -h <broker-ip> -t test -m "hello"`

---

## 6. Kiosk mode on the 7" touchscreen (Chromium)

Chromium will auto-launch in full-screen kiosk mode showing the sauna UI on boot.

Edit the LXDE autostart file:

```bash
mkdir -p /home/pi/.config/lxsession/LXDE-pi
nano /home/pi/.config/lxsession/LXDE-pi/autostart
```

Add or ensure these lines are present:

```text
@xset s off
@xset -dpms
@xset s noblank

@chromium-browser --kiosk --incognito http://localhost:5000
```

- `xset` lines disable screen blanking/power management
- Chromium launches in full-screen kiosk mode pointing to localhost:5000
- `--incognito` prevents browser from storing cookies/cache

Reboot to test:

```bash
sudo reboot
```

After boot, the backend service should be running and Chromium should show the sauna UI automatically. The touchscreen is now interactive for setting temperatures, enabling/disabling the heater, and configuring sensors and MQTT.

---

## 7. Optional: Desktop shortcut to start the app manually

If you want a one-click desktop icon to start the backend manually (useful for debugging):

```bash
mkdir -p /home/pi/Desktop
nano /home/pi/Desktop/SmartSauna.desktop
```

Paste:

```ini
[Desktop Entry]
Type=Application
Name=Smart Sauna Controller
Comment=Start the Smart Sauna Flask app
Exec=/home/pi/sauna_controller/run_sauna.sh
Icon=utilities-terminal
Terminal=true
Categories=Utility;
```

Make it executable:

```bash
chmod +x /home/pi/Desktop/SmartSauna.desktop
```

Now you can double-click **Smart Sauna Controller** on the Pi desktop to start the app manually.

---

## 8. Remote Access

With the service running and `main.py` binding to `0.0.0.0`, any device on your network can access the UI via:

```text
http://<pi-ip-address>:5000
```

Get your Pi's IP:
```bash
hostname -I
```

You can also access from your phone/laptop on the same WiFi or wired network. For production, consider:
- Setting a static IP on the Pi (via `/etc/dhcpcd.conf`)
- Using a DNS name (e.g., `sauna.local` via mDNS / Avahi)
- Tunneling via VPN or reverse proxy for external access

---

## 9. Configuration File Reference

All settings are saved to `sauna_state.json` in the project directory. You can edit manually (with the service stopped) if needed.

**Example single-sensor DS18B20 config:**
```json
{
  "desired_temp": 80,
  "current_temp": 45,
  "heater_enabled": false,
  "thermometer_mode": "single",
  "sensor_type": "ds18b20",
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

**Example dual-sensor DS18B20 config:**
```json
{
  "desired_temp": 80,
  "current_temp": 75,
  "thermometer_mode": "dual",
  "sensor_type": "ds18b20",
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

**Example thermocouple config (dual-sensor):**
```json
{
  "thermometer_mode": "dual",
  "sensor_type": "thermocouple",
  "goal_spi_cs": 8,
  "limit_spi_cs": 7,
  "limit_temp": 100
}
```

---

## 10. Troubleshooting

### No temperature reading (shows "N/A" on dash)

**Check 1-Wire (if using DS18B20):**
```bash
ls -la /sys/bus/w1/devices/ | grep 28-
```
- No 28-* entries? 1-Wire not enabled. See "Prepare 1-Wire" section.
- Entries present but app shows N/A? Sensor ID mismatch. Check config in UI.

**Check SPI (if using thermocouple):**
```bash
ls /dev/spidev*
```
- Should see `spidev0.0` or similar. If not, enable SPI:
  ```bash
  sudo raspi-config
  # Interface Options → SPI → Yes → Reboot
  ```
- Verify wiring: CLK→GPIO11, MOSI→GPIO10, MISO→GPIO9, CS→GPIO8/7, VCC→3.3V, GND→GND

### Heater won't turn on

**Check relay wiring:**
- Confirm GPIO 17 is wired to relay module input
- Test manually: `gpio -g mode 17 out; gpio -g write 17 1` (should enable relay)
- If no relay click, check Pi GPIO with multimeter (should read ~3.3V when on)

**Check app logs:**
```bash
journalctl -u sauna.service | tail -20
```
- Look for errors about GPIO or lockout reasons

**Check lockout:**
- If "Lockout" appears under heater status, temperature may be at or above 105°C
- Press "Confirm Continue" if this is a false alarm / test situation

### MQTT not connecting

**Verify broker is reachable:**
```bash
ping <broker-ip>
```

**Test MQTT on broker:**
```bash
mosquitto_pub -h <broker-ip> -p 1883 -t test -m "hello"
```

**If using Home Assistant Mosquitto add-on:**
- Broker IP is typically `localhost` (from Pi or network IP if on different host)
- Enable "Anonymous access" in HA MQTT add-on, or create HA user in add-on settings
- Check HA logs for MQTT connection errors

**Check systemd logs:**
```bash
journalctl -u sauna.service | grep -i mqtt
```

### Kiosk mode not showing

**Ensure service is running:**
```bash
sudo systemctl status sauna.service
```

**Check Chromium is installed:**
```bash
which chromium-browser
```
- If not found, install: `sudo apt-get install chromium-browser`

**Manual test:**
```bash
DISPLAY=:0 chromium-browser --kiosk --incognito http://localhost:5000 &
```

**Kill any existing Chromium instances if stuck:**
```bash
pkill -f chromium-browser
```

### Configuration not persisting after restart

**Ensure service has write permissions:**
```bash
ls -la /home/pi/sauna_controller/sauna_state.json
```
- Should show `pi` as owner. If not:
  ```bash
  sudo chown pi:pi /home/pi/sauna_controller/sauna_state.json
  ```

**Check disk space:**
```bash
df -h /home
```
- If nearly full (<5% free), JSON may not save. Free up space.

### Dual-sensor mode not activating (limit sensor ignored)

- Check "Thermometer Setup" widget mode is set to `Dual` (not `Single`)
- Click "Save Thermometer Setup" to persist
- Refresh browser page
- Check both sensor IDs / SPI CS pins are entered correctly

### Temperature reading jumping around / unreliable

**For DS18B20:**
- Check pull-up resistor (4.7kΩ) is properly wired between GPIO 4 and 3.3V
- Try shorter/higher-quality wires
- If reading is way off, sensor may be failing; test with `cat /sys/bus/w1/devices/28-*/w1_slave`

**For thermocouple:**
- Verify power supply is stable (3.3V should not droop below 3.0V under load)
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
