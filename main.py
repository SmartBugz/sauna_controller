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


@app.route("/heater/on", methods=["POST"])
def heater_on():
    """Explicitly enable heater operation (allows bang-bang control to drive relay)."""
    controller.set_heater_enabled(True)
    return redirect(url_for("index"))


@app.route("/heater/off", methods=["POST"])
def heater_off():
    """Explicitly disable heater operation and turn relay off."""
    controller.set_heater_enabled(False)
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


if __name__ == "__main__":
    # NOTE: For development only. In production, use gunicorn/uwsgi.
    # host="0.0.0.0" makes it accessible from other devices on the network.
    app.run(host="0.0.0.0", port=5000, debug=True)
