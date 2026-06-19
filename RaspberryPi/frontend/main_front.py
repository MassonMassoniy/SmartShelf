from __future__ import annotations

import json
import os
import time
from typing import Any
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import urlopen

from flask import Flask, jsonify, render_template, request

app = Flask(__name__, template_folder='templates')

BACKEND_BASE_URL = os.getenv("SMARTSHELF_BACKEND_URL", "http://127.0.0.1:8000")

# Last successful backend snapshot; used when backend is temporarily unavailable.
_LAST_SNAPSHOT: dict[str, Any] = {
	"sensors": [],
	"readings": {},
}

def _build_url(path: str, params: dict[str, Any] | None = None) -> str:
	url = f"{BACKEND_BASE_URL.rstrip('/')}/{path.lstrip('/')}"
	if params:
		url = f"{url}?{urlencode(params)}"
	return url

def _fetch_json(path: str, params: dict[str, Any] | None = None, timeout: float = 1.5) -> Any:
	url = _build_url(path, params=params)
	with urlopen(url, timeout=timeout) as resp:
		return json.loads(resp.read().decode("utf-8"))

def _map_node_reading(node: dict[str, Any]) -> dict[str, Any]:
	return {
		"gas": node.get("voc_index"),
		"temperature": node.get("temperature_c"),
		"humidity": node.get("humidity_rh"),
		"ts": int(node.get("received_at") or time.time()),
		"node_id": node.get("node_id"),
		"address": node.get("address"),
		"name": node.get("name"),
	}

def _load_latest_snapshot() -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
	nodes = _fetch_json("/nodes/latest")
	sensors: list[dict[str, Any]] = []
	readings: dict[str, dict[str, Any]] = {}

	for node in nodes:
		node_id = node.get("node_id")
		if node_id is None:
			continue
		sensor_id = f"node-{int(node_id)}"
		sensor_name = node.get("name") or f"ESP32 #{int(node_id)}"
		sensors.append({"id": sensor_id, "name": sensor_name, "node_id": int(node_id)})
		readings[sensor_id] = _map_node_reading(node)

	# Keep order stable.
	sensors.sort(key=lambda s: s["node_id"])
	return sensors, readings

def _get_snapshot_with_fallback() -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
	global _LAST_SNAPSHOT
	try:
		sensors, readings = _load_latest_snapshot()
		_LAST_SNAPSHOT = {"sensors": sensors, "readings": readings}
		return sensors, readings
	except (TimeoutError, URLError, OSError, ValueError):
		return _LAST_SNAPSHOT["sensors"], _LAST_SNAPSHOT["readings"]

@app.route("/")
def index():
	sensors, readings = _get_snapshot_with_fallback()
	return render_template("index.html", sensors=sensors, readings=readings)

@app.route("/sensor/<sensor_id>/tile")
def sensor_tile(sensor_id):
	sensors, readings = _get_snapshot_with_fallback()
	reading = readings.get(sensor_id, {"gas": None, "temperature": None, "humidity": None, "ts": int(time.time())})
	sensor = next((s for s in sensors if s['id'] == sensor_id), {"id": sensor_id, "name": sensor_id})
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
	_, data = _get_snapshot_with_fallback()
	return jsonify(data)

def main_front():
	app.run(debug=True, host='127.0.0.1', port=5000)

if __name__ == "__main__":
	main_front()