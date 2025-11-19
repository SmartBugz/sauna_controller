# Raspberry Pi Setup for Smart Sauna Controller

This guide shows how to:

- Run the Flask app as a background service that restarts after reboots or power loss.
- Launch the UI automatically in kiosk mode on the Pi 7" touchscreen.
- Create a simple desktop shortcut to start the app manually if desired.

> All paths below assume the project is cloned to `/home/pi/sauna_controller` and the virtualenv is at `/home/pi/sauna_controller/.venv`.

---

## 1. Create a run script for the app

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

This script will be used both by the service and any shortcuts.

---

## 2. Auto-start the backend with systemd

Create a systemd service so the app starts automatically on boot and restarts after crashes or power outages.

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

- `enable` makes it run on every boot.
- `Restart=always` ensures it comes back automatically after failures.

To stop and disable later:

```bash
sudo systemctl stop sauna.service
sudo systemctl disable sauna.service
```

---

## 3. Kiosk mode on the 7" touchscreen (Chromium)

Assuming the Pi boots into the desktop (LXDE / Raspberry Pi OS), you can auto-launch Chromium in kiosk mode to show the UI on the touchscreen.

Edit the autostart file for the `pi` user:

```bash
mkdir -p /home/pi/.config/lxsession/LXDE-pi
nano /home/pi/.config/lxsession/LXDE-pi/autostart
```

Add lines (or append to existing):

```text
@xset s off
@xset -dpms
@xset s noblank

@chromium-browser --kiosk --incognito http://localhost:5000
```

- `xset` lines disable screen blanking and power management.
- Chromium opens in full-screen kiosk mode pointing to the Flask app.

Reboot to test:

```bash
sudo reboot
```

After boot, the backend service should be running and the browser should show the sauna UI automatically.

---

## 4. Optional: Desktop shortcut to start the app manually

If you also want a one-click desktop icon on the Pi to start the backend (for manual use or debugging), create a `.desktop` file.

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

You can now double-click **Smart Sauna Controller** on the Pi desktop to start the app manually (useful if you temporarily stop the systemd service or run in dev mode).

---

## 5. Remote access

With the service running and `main.py` binding to `0.0.0.0`, any device on your network can access the UI via:

```text
http://<pi-ip-address>:5000
```

Optionally expose this via VPN or reverse proxy if you want access from outside your LAN.
