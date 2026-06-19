from flask import Flask, render_template, jsonify
import random
import time

app = Flask(__name__, template_folder='templates')

SENSORS = [
	{"id": "esp32-1", "name": "ESP32 #1"},
	{"id": "esp32-2", "name": "ESP32 #2"},
	{"id": "esp32-3", "name": "ESP32 #3"},
]


def fake_reading():
	return {
		"gas": round(random.uniform(200, 600), 1),
		"temperature": round(random.uniform(18, 30), 1),
		"humidity": round(random.uniform(30, 70), 1),
		"ts": int(time.time()),
	}


@app.route("/")
def index():
	readings = {s['id']: fake_reading() for s in SENSORS}
	return render_template("index.html", sensors=SENSORS, readings=readings)


from flask import request


@app.route("/sensor/<sensor_id>/tile")
def sensor_tile(sensor_id):
	reading = fake_reading()
	sensor = next((s for s in SENSORS if s['id'] == sensor_id), {"id": sensor_id, "name": sensor_id})
	variant = request.args.get('variant', 'default')
	template_map = {
		'default': '_tile.html',
		'gauge': '_tile_gauge.html',
		'line': '_tile_line.html',
	}
	template = template_map.get(variant, '_tile.html')
	return render_template(template, sensor=sensor, reading=reading)


@app.route("/api/sensors")
def sensors_api():
	data = {s['id']: fake_reading() for s in SENSORS}
	return jsonify(data)


if __name__ == "__main__":
	app.run(debug=True, host='127.0.0.1', port=5000)
