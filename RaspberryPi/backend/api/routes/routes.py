from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, render_template, request, stream_with_context

from ...services.backend_service import BackendService

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
LIVE_RELOAD_RELATIVE_DIRS = (
    "RaspberryPi/backend",
    "RaspberryPi/frontend/templates",
)
LIVE_RELOAD_SUFFIXES = {".py", ".html", ".css", ".js"}
LIVE_RELOAD_EXCLUDED_DIRS = {"__pycache__", ".git", ".venv", "venv", "node_modules", "data"}


def create_api(service: BackendService, app: Flask, lifespan=None) -> Flask:
    last_snapshot: dict[str, Any] = {"sensors": [], "readings": {}}
    live_reload_enabled = bool(app.config.get("SMARTSHELF_LIVE_RELOAD", False))
    repo_root_raw = app.config.get("SMARTSHELF_REPO_ROOT")
    repo_root = Path(str(repo_root_raw)) if repo_root_raw else None
    live_reload_roots = _resolve_live_reload_roots(repo_root)

    def parse_window_seconds():
        window_seconds = request.args.get("window_seconds", default=600, type=int)
        if window_seconds < 1 or window_seconds > 3600:
            return None, (jsonify({"detail": "window_seconds must be between 1 and 3600"}), 422)
        return window_seconds, None

    def parse_minute_history_query():
        outliers = request.args.get("outliers", default="include", type=str)
        if outliers not in {"include", "exclude"}:
            return None, None, None, None, (jsonify({"detail": "outliers must be 'include' or 'exclude'"}), 422)
        from_ts = request.args.get("from_ts", default=None, type=int)
        to_ts = request.args.get("to_ts", default=None, type=int)
        node_id = request.args.get("node_id", default=None, type=int)
        return outliers, from_ts, to_ts, node_id, None

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
            if last_snapshot["sensors"]:
                sensors, readings = last_snapshot["sensors"], last_snapshot["readings"]
            else:
                sensors, readings = _default_snapshot()

        return render_template(
            "index.html",
            sensors=sensors,
            readings=readings,
            backend_stream_url="/nodes/stream",
            backend_api_base_url="",
            stale_seconds=service.stale_seconds,
            dev_live_reload_enabled=live_reload_enabled,
        )

    @app.route("/sensor/<sensor_id>/tile")
    def sensor_tile(sensor_id: str) -> str:
        nonlocal last_snapshot
        try:
            sensors, readings = _load_latest_snapshot(service)
            last_snapshot = {"sensors": sensors, "readings": readings}
        except Exception:
            if last_snapshot["sensors"]:
                sensors, readings = last_snapshot["sensors"], last_snapshot["readings"]
            else:
                sensors, readings = _default_snapshot()

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
        window_seconds, error = parse_window_seconds()
        if error is not None:
            return error

        return jsonify(
            {
                "window_seconds": window_seconds,
                "nodes": service.get_realtime_window(window_seconds=window_seconds),
            }
        )

    @app.get("/nodes/realtime/stream")
    def nodes_realtime_stream() -> tuple[Response, int] | Response:
        window_seconds, error = parse_window_seconds()
        if error is not None:
            return error

        def event_generator():
            try:
                while True:
                    payload = {
                        "server_time": int(time.time()),
                        "window_seconds": window_seconds,
                        "nodes": service.get_realtime_window(window_seconds=window_seconds),
                    }
                    yield f"data: {json.dumps(payload)}\n\n"
                    time.sleep(1)
            except GeneratorExit:
                return

        return _make_sse_response(event_generator())

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

        return _make_sse_response(event_generator())

    @app.get("/nodes/minute-history")
    def minute_history() -> tuple[Response, int] | Response:
        outliers, from_ts, to_ts, node_id, error = parse_minute_history_query()
        if error is not None:
            return error

        return jsonify(
            service.storage.get_minute_history(
                outliers=outliers,
                from_ts=from_ts,
                to_ts=to_ts,
                node_id=node_id,
            )
        )

    @app.get("/dev/live-reload/stream")
    def dev_live_reload_stream() -> tuple[Response, int] | Response:
        if not live_reload_enabled:
            return jsonify({"detail": "live reload disabled"}), 404
        if not live_reload_roots:
            return jsonify({"detail": "live reload paths unavailable"}), 500

        def event_generator():
            last_signature = _live_reload_signature(live_reload_roots)
            yield ": connected\n\n"
            try:
                while True:
                    signature = _live_reload_signature(live_reload_roots)
                    if signature != last_signature:
                        payload = {"ts": int(time.time())}
                        yield f"event: reload\ndata: {json.dumps(payload)}\n\n"
                        last_signature = signature
                    else:
                        yield ": keepalive\n\n"
                    time.sleep(1)
            except GeneratorExit:
                return

        return _make_sse_response(event_generator())

    return app


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


def _load_latest_snapshot(service: BackendService) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    nodes = service.get_latest_nodes()
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


def _make_sse_response(event_iterator: Any) -> Response:
    return Response(
        stream_with_context(event_iterator),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _resolve_live_reload_roots(repo_root: Path | None) -> list[Path]:
    if repo_root is None:
        return []
    roots: list[Path] = []
    for relative in LIVE_RELOAD_RELATIVE_DIRS:
        path = (repo_root / relative).resolve()
        if path.exists():
            roots.append(path)
    return roots


def _live_reload_signature(roots: list[Path]) -> tuple[int, int]:
    file_count = 0
    latest_mtime_ns = 0

    for root in roots:
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix.lower() not in LIVE_RELOAD_SUFFIXES:
                continue
            if any(part in LIVE_RELOAD_EXCLUDED_DIRS for part in path.parts):
                continue
            try:
                mtime_ns = path.stat().st_mtime_ns
            except OSError:
                continue
            file_count += 1
            if mtime_ns > latest_mtime_ns:
                latest_mtime_ns = mtime_ns

    return file_count, latest_mtime_ns


