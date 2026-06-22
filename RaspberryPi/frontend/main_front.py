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
BACKEND_STREAM_URL = os.getenv("SMARTSHELF_BACKEND_STREAM_URL", f"{BACKEND_BASE_URL.rstrip('/')}/nodes/stream")
STALE_SECONDS = float(os.getenv("SMARTSHELF_STALE_SECONDS", "4"))

SENSOR_LABEL_BY_NODE_ID = {
	3073: "C3",
	3077: "A1",
	3165: "C2",
	3197: "A2",
	16341: "A3",
	16645: "B1",
	16681: "B2",
	23109: "B3",
	36237: "C1",
}
SENSOR_LABEL_ORDER = ["A1", "A2", "A3", "B1", "B2", "B3", "C1", "C2", "C3"]
NODE_ID_BY_SENSOR_LABEL = {label: node_id for node_id, label in SENSOR_LABEL_BY_NODE_ID.items()}

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

def _empty_reading(node_id: int) -> dict[str, Any]:
	return {
		"gas": None,
		"temperature": None,
		"humidity": None,
		"ts": None,
		"node_id": node_id,
		"address": None,
		"name": None,
	}

def _sensor_sort_key(sensor: dict[str, Any]) -> tuple[int, int]:
	label = sensor.get("label")
	if label in SENSOR_LABEL_ORDER:
		return SENSOR_LABEL_ORDER.index(label), int(sensor.get("node_id") or 0)
	return len(SENSOR_LABEL_ORDER), int(sensor.get("node_id") or 0)

def _build_sensor(node_id: int) -> dict[str, Any]:
	label = SENSOR_LABEL_BY_NODE_ID.get(node_id)
	if label is None:
		label = f"Unmapped #{node_id}"
		name = label
		is_expected = False
	else:
		name = label
		is_expected = True
	return {
		"id": f"node-{node_id}",
		"name": name,
		"label": label,
		"node_id": node_id,
		"is_expected": is_expected,
	}

def _default_snapshot() -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
	sensors = [_build_sensor(node_id) for node_id in SENSOR_LABEL_BY_NODE_ID]
	readings = {f"node-{node_id}": _empty_reading(node_id) for node_id in SENSOR_LABEL_BY_NODE_ID}
	sensors.sort(key=_sensor_sort_key)
	return sensors, readings

def _load_latest_snapshot() -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
	nodes = _fetch_json("/nodes/latest")
	readings: dict[str, dict[str, Any]] = {}
	seen_node_ids: set[int] = set()

	for node in nodes:
		node_id = node.get("node_id")
		if node_id is None:
			continue
		node_id = int(node_id)
		sensor_id = f"node-{node_id}"
		seen_node_ids.add(node_id)
		readings[sensor_id] = _map_node_reading(node)

	expected_node_ids = set(SENSOR_LABEL_BY_NODE_ID.keys())
	sensors = [_build_sensor(node_id) for node_id in expected_node_ids | seen_node_ids]
	for node_id in expected_node_ids:
		readings.setdefault(f"node-{node_id}", _empty_reading(node_id))

	sensors.sort(key=_sensor_sort_key)
	return sensors, readings

def _get_snapshot_with_fallback() -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
	global _LAST_SNAPSHOT
	try:
		sensors, readings = _load_latest_snapshot()
		_LAST_SNAPSHOT = {"sensors": sensors, "readings": readings}
		return sensors, readings
	except (TimeoutError, URLError, OSError, ValueError):
		if _LAST_SNAPSHOT["sensors"]:
			return _LAST_SNAPSHOT["sensors"], _LAST_SNAPSHOT["readings"]
		return _default_snapshot()

@app.route("/")
def index():
	sensors, readings = _get_snapshot_with_fallback()
	return render_template(
		"index.html",
		sensors=sensors,
		readings=readings,
		backend_stream_url=BACKEND_STREAM_URL,
		backend_api_base_url="",
		stale_seconds=STALE_SECONDS,
	)

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

@app.get("/nodes/realtime")
def nodes_realtime():
	window_seconds = request.args.get("window_seconds", default=600, type=int)
	return jsonify(_fetch_json("/nodes/realtime", params={"window_seconds": window_seconds}))

@app.get("/nodes/minute-history")
def minute_history():
	params: dict[str, Any] = {
		"outliers": request.args.get("outliers", default="include", type=str),
	}
	for key in ("from_ts", "to_ts", "node_id"):
		value = request.args.get(key, default=None, type=int)
		if value is not None:
			params[key] = value
	return jsonify(_fetch_json("/nodes/minute-history", params=params, timeout=8))

def main_front():
	app.run(debug=True, host='127.0.0.1', port=5000)

if __name__ == "__main__":
	main_front()