"""Flask web application entry point for the smart sauna controller.

- Exposes web UI for monitoring and controlling the sauna
- Shares state with the background SaunaController via a singleton instance
"""

import subprocess

from flask import Flask, render_template, request, redirect, url_for, jsonify

from sauna_controller import SaunaController


# Create a single global Flask app and SaunaController instance
app = Flask(__name__)
controller = SaunaController()


@app.route("/", methods=["GET"])
def index():
    """Render the main dashboard with current sauna status and controls."""
    state = controller.get_state_snapshot()
    return render_template("index.html", state=state)


@app.route("/api/status", methods=["GET"])
def api_status():
    """Lightweight JSON status for live UI updates."""
    return jsonify(controller.get_state_snapshot())


@app.route("/heater/toggle", methods=["POST"])
def heater_toggle():
    """Toggle heater enable flag based on current state.

    This is the single user-facing control: when enabled, the background
    controller will manage the relay to track the desired temperature.
    When disabled, the relay is forced off.
    """
    state = controller.get_state_snapshot()
    current_enabled = bool(state.get("heater_enabled", False))
    controller.set_heater_enabled(not current_enabled)
    return redirect(url_for("index"))


@app.route("/setpoint", methods=["POST"])
def update_setpoint():
    """Update the desired temperature setpoint and persist it to disk."""
    try:
        desired = float(request.form.get("desired_temp"))
    except (TypeError, ValueError):
        # Ignore invalid input and just reload the page
        return redirect(url_for("index"))

    controller.set_desired_temperature(desired)
    return redirect(url_for("index"))


@app.route("/units/toggle", methods=["POST"])
def toggle_units():
    """Toggle between imperial and metric display units.

    This only changes how values are displayed and how new setpoints are
    interpreted; the underlying control logic always uses Celsius.
    """
    controller.toggle_units()
    return redirect(url_for("index"))


@app.route("/heater/confirm_continue", methods=["POST"])
def heater_confirm_continue():
    """User confirms continuing after max-on-time safety window.

    Clears confirmation/lockout flags and allows the control loop to
    continue operating towards the desired setpoint.
    """
    controller.confirm_continue()
    return redirect(url_for("index"))


@app.route("/limit_setpoint", methods=["POST"])
def update_limit_setpoint():
    """Update the limit-sensor temperature cutoff used in dual mode."""
    try:
        limit_temp = float(request.form.get("limit_temp"))
    except (TypeError, ValueError):
        return redirect(url_for("index"))

    controller.set_limit_temp(limit_temp)
    return redirect(url_for("index"))


@app.route("/thermometer/config", methods=["POST"])
def thermometer_config():
    """Update thermometer mode, sensor type, and per-sensor identifiers."""
    mode = request.form.get("thermometer_mode", "single")
    goal_sensor_type = request.form.get("goal_sensor_type", "ds18b20")
    limit_sensor_type = request.form.get("limit_sensor_type", "ds18b20")

    controller.set_thermometer_mode(mode)
    controller.set_goal_sensor_type(goal_sensor_type)
    controller.set_limit_sensor_type(limit_sensor_type)

    # DS18B20 mapping
    bench_sensor_id = request.form.get("bench_sensor_id")
    ceiling_sensor_id = request.form.get("ceiling_sensor_id")
    controller.set_sensor_ids(bench_sensor_id, ceiling_sensor_id)

    # Thermocouple SPI CS mapping
    try:
        goal_spi_cs = int(request.form.get("goal_spi_cs", "8"))
        limit_spi_cs = int(request.form.get("limit_spi_cs", "7"))
        controller.set_spi_pins(goal_spi_cs, limit_spi_cs)
    except (TypeError, ValueError):
        pass

    return redirect(url_for("index"))


@app.route("/mqtt/config", methods=["POST"])
def mqtt_config():
    """Update Home Assistant MQTT integration settings."""
    enabled = request.form.get("mqtt_enabled") == "on"
    broker = (request.form.get("mqtt_broker") or "").strip()
    username = (request.form.get("mqtt_username") or "").strip()
    password = request.form.get("mqtt_password") or ""

    try:
        port = int(request.form.get("mqtt_port", "1883"))
    except (TypeError, ValueError):
        port = 1883

    controller.set_mqtt_config(
        enabled=enabled,
        broker=broker,
        port=port,
        username=username,
        password=password,
    )
    return redirect(url_for("index"))


@app.route("/schedule", methods=["POST"])
def set_schedule():
    """Set or clear a simple one-shot schedule.

    Expects an ISO datetime-local string in the form field "schedule_time".
    For now we trust the client's clock; in a real system we'd handle
    time zones explicitly. We enforce a maximum horizon of 48 hours.
    """
    value = request.form.get("schedule_time")
    if not value:
        controller.set_schedule(None)
        return redirect(url_for("index"))

    try:
        # datetime-local comes as 'YYYY-MM-DDTHH:MM'
        from datetime import datetime

        dt = datetime.fromisoformat(value)
        now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
        delta = dt - now
        max_horizon_sec = 48 * 3600
        if delta.total_seconds() <= 0 or delta.total_seconds() > max_horizon_sec:
            # Out of allowed window; clear schedule
            controller.set_schedule(None)
        else:
            controller.set_schedule(now.timestamp() + delta.total_seconds())
    except Exception:
        # On parse error, just clear schedule
        controller.set_schedule(None)

    return redirect(url_for("index"))


@app.route("/cost_config", methods=["POST"])
def cost_config():
    """Update electricity cost and heater power configuration."""
    def _parse_float(name: str):
        raw = request.form.get(name)
        if not raw:
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    price = _parse_float("price_per_kwh")
    power = _parse_float("heater_power_kw")
    controller.set_cost_config(price, power)
    return redirect(url_for("index"))


@app.route("/timer/mode", methods=["POST"])
def timer_mode():
    """Set timer widget mode to stopwatch or timer."""
    mode = request.form.get("mode", "stopwatch")
    controller.timer_set_mode(mode)
    return redirect(url_for("index"))


@app.route("/timer/preset", methods=["POST"])
def timer_preset():
    """Set a preset duration in minutes for timer mode."""
    try:
        minutes = int(request.form.get("minutes", "0"))
    except ValueError:
        minutes = 0
    controller.timer_set_duration_minutes(minutes)
    return redirect(url_for("index"))


@app.route("/timer/custom", methods=["POST"])
def timer_custom():
    """Set a custom duration in minutes for timer mode."""
    try:
        minutes = int(request.form.get("custom_minutes", "0"))
    except ValueError:
        minutes = 0
    controller.timer_set_duration_minutes(minutes)
    return redirect(url_for("index"))


@app.route("/timer/start", methods=["POST"])
def timer_start():
    controller.timer_start()
    return redirect(url_for("index"))


@app.route("/timer/stop", methods=["POST"])
def timer_stop():
    controller.timer_stop()
    return redirect(url_for("index"))


@app.route("/timer/reset", methods=["POST"])
def timer_reset():
    controller.timer_reset()
    return redirect(url_for("index"))


@app.route("/kiosk/exit", methods=["POST"])
def kiosk_exit():
    """Exit Chromium kiosk mode on the Pi.

    We run the kill command with a short delay so this request can return
    before the browser process exits.
    """
    try:
        subprocess.Popen(
            [
                "bash",
                "-lc",
                "sleep 1; pkill -f 'chromium.*--kiosk' >/dev/null 2>&1 || pkill -f chromium >/dev/null 2>&1 || true",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass

    return redirect(url_for("index"))


if __name__ == "__main__":
    # host="0.0.0.0" makes it accessible from other devices on the network.
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
