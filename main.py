"""Flask web application entry point for the smart sauna controller.

- Exposes web UI for monitoring and controlling the sauna
- Shares state with the background SaunaController via a singleton instance
"""

from flask import Flask, render_template, request, redirect, url_for

from sauna_controller import SaunaController


# Create a single global Flask app and SaunaController instance
app = Flask(__name__)
controller = SaunaController()


@app.route("/", methods=["GET"])
def index():
    """Render the main dashboard with current sauna status and controls."""
    state = controller.get_state_snapshot()
    return render_template("index.html", state=state)


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
    # Re-enable heater and clear confirmation flags via the controller API.
    # For now we simply re-enable; controller will handle ensuring safety
    # limits (max temperature, further runtime checks) remain in effect.
    controller.set_heater_enabled(True)
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


if __name__ == "__main__":
    # NOTE: For development only. In production, use gunicorn/uwsgi.
    # host="0.0.0.0" makes it accessible from other devices on the network.
    app.run(host="0.0.0.0", port=5000, debug=True)
