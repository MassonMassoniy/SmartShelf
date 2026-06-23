from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from flask import Flask, Response, jsonify, render_template, request, stream_with_context

from ...services.backend_service import BackendService


def create_api(service: BackendService, app: Flask, lifespan=None) -> Flask:
    last_snapshot: dict[str, Any] = {"sensors": [], "readings": {}}

    @app.after_request
    def add_cors_headers(response: Response) -> Response:
        origin = request.headers.get("Origin")
        if origin in {"http://127.0.0.1:5000", "http://localhost:5000"}:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Credentials"] = "true"
            response.headers["Access-Control-Allow-Methods"] = "GET,POST,PUT,DELETE,OPTIONS"
            response.headers["Access-Control-Allow-Headers"] = "*"
        return response

    @app.route("/")
    def index() -> str:
        nonlocal last_snapshot
        try:
            sensors, readings = _load_latest_snapshot(service)
            last_snapshot = {"sensors": sensors, "readings": readings}
        except Exception:
            sensors, readings = last_snapshot["sensors"], last_snapshot["readings"]

        return render_template(
            "index.html",
            sensors=sensors,
            readings=readings,
            backend_stream_url="/nodes/stream",
        )

    @app.route("/sensor/<sensor_id>/tile")
    def sensor_tile(sensor_id: str) -> str:
        nonlocal last_snapshot
        try:
            sensors, readings = _load_latest_snapshot(service)
            last_snapshot = {"sensors": sensors, "readings": readings}
        except Exception:
            sensors, readings = last_snapshot["sensors"], last_snapshot["readings"]

        reading = readings.get(
            sensor_id,
            {"gas": None, "temperature": None, "humidity": None, "ts": int(time.time())},
        )
        sensor = next((s for s in sensors if s["id"] == sensor_id), {"id": sensor_id, "name": sensor_id})
        variant = request.args.get("variant", "default")
        template_map = {
            "default": "_tile.html",
            "gauge": "_tile_gauge.html",
            "line": "_tile_line.html",
        }
        template = template_map.get(variant, "_tile.html")
        return render_template(template, sensor=sensor, reading=reading)

    @app.route("/api/sensors")
    def sensors_api() -> Response:
        nonlocal last_snapshot
        try:
            _, readings = _load_latest_snapshot(service)
            last_snapshot["readings"] = readings
        except Exception:
            readings = last_snapshot["readings"]
        return jsonify(readings)

    @app.get("/health")
    def health() -> Response:
        return jsonify(service.get_health())

    @app.get("/nodes/latest")
    def nodes_latest() -> Response:
        return jsonify(service.get_latest_nodes())

    @app.get("/nodes/realtime")
    def nodes_realtime() -> tuple[Response, int] | Response:
        window_seconds = request.args.get("window_seconds", default=600, type=int)
        if window_seconds < 1 or window_seconds > 3600:
            return jsonify({"detail": "window_seconds must be between 1 and 3600"}), 422

        return jsonify(
            {
                "window_seconds": window_seconds,
                "nodes": service.get_realtime_window(window_seconds=window_seconds),
            }
        )

    @app.get("/nodes/status")
    def nodes_status() -> Response:
        return jsonify(service.get_node_status())

    @app.get("/nodes/stream")
    def nodes_stream() -> Response:
        def event_generator():
            try:
                while True:
                    payload = {
                        "server_time": int(time.time()),
                        "nodes": service.get_latest_nodes(),
                    }
                    # SSE format: each event is separated by a blank line.
                    yield f"data: {json.dumps(payload)}\n\n"
                    time.sleep(1)
            except GeneratorExit:
                return

        return Response(
            stream_with_context(event_generator()),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/nodes/minute-history")
    def minute_history() -> tuple[Response, int] | Response:
        outliers = request.args.get("outliers", default="include", type=str)
        if outliers not in {"include", "exclude"}:
            return jsonify({"detail": "outliers must be 'include' or 'exclude'"}), 422

        from_ts = request.args.get("from_ts", default=None, type=int)
        to_ts = request.args.get("to_ts", default=None, type=int)
        node_id = request.args.get("node_id", default=None, type=int)
        return jsonify(
            service.storage.get_minute_history(
                outliers=outliers,
                from_ts=from_ts,
                to_ts=to_ts,
                node_id=node_id,
            )
        )

def _load_latest_snapshot(service: BackendService) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    nodes = service.get_latest_nodes()
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

    sensors.sort(key=lambda s: s["node_id"])
    return sensors, readings

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