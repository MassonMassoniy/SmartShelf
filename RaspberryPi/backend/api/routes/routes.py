from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, render_template, request, stream_with_context

from ...services.backend_service import BackendService
from ...services.cell_config_service import CellConfigError
from ...services.zone_evaluator import classify_zone, zone_badge_class, zone_bar_class
from ...config.constants import PROJECT_NODE_IDS

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
                sensors, readings = _default_snapshot(service)

        return render_template(
            "index.html",
            sensors=sensors,
            readings=readings,
            backend_stream_url="/nodes/stream",
            backend_api_base_url="",
            stale_seconds=service.stale_seconds,
            dev_live_reload_enabled=live_reload_enabled,
            categories=service.cell_config.list_categories(),
            cell_configs={cfg["node_id"]: cfg for cfg in service.cell_config.get_all_cell_configs()},
            metric_limits=service.cell_config.get_metric_limits(),
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
                sensors, readings = _default_snapshot(service)

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

    @app.get("/api/categories")
    def categories_api() -> Response:
        return jsonify(
            {
                "categories": service.cell_config.list_categories(),
                "metric_limits": service.cell_config.get_metric_limits(),
            }
        )

    @app.get("/api/product-profiles")
    def product_profiles_api() -> Response:
        return jsonify({"profiles": service.cell_config.list_profiles()})

    @app.post("/api/product-profiles")
    def create_product_profile() -> tuple[Response, int] | Response:
        try:
            profile = service.cell_config.create_profile(request.get_json(silent=True) or {})
        except CellConfigError as exc:
            return jsonify({"detail": str(exc)}), 422
        return jsonify(profile), 201

    @app.get("/api/product-profiles/<profile_id>")
    def get_product_profile(profile_id: str) -> tuple[Response, int] | Response:
        profile = service.cell_config.get_profile(profile_id)
        if profile is None:
            return jsonify({"detail": f"Unknown profile_id {profile_id!r}"}), 404
        return jsonify(profile)

    @app.put("/api/product-profiles/<profile_id>")
    def update_product_profile(profile_id: str) -> tuple[Response, int] | Response:
        try:
            profile = service.cell_config.update_profile(
                profile_id, request.get_json(silent=True) or {}
            )
        except CellConfigError as exc:
            status = 404 if str(exc).startswith("Unknown profile_id") else 422
            return jsonify({"detail": str(exc)}), status
        return jsonify(profile)

    @app.delete("/api/product-profiles/<profile_id>")
    def delete_product_profile(profile_id: str) -> tuple[Response, int] | Response:
        try:
            service.cell_config.delete_profile(profile_id)
        except CellConfigError as exc:
            message = str(exc)
            status = 404 if message.startswith("Unknown profile_id") else 409
            return jsonify({"detail": message}), status
        return jsonify({"deleted": True, "profile_id": profile_id})

    @app.get("/api/cells/config")
    def cells_config_api() -> Response:
        return jsonify({"cells": service.cell_config.get_all_cell_configs()})

    @app.put("/api/cells/<int:node_id>/config")
    def update_cell_config(node_id: int) -> tuple[Response, int] | Response:
        body = request.get_json(silent=True) or {}
        mode = body.get("assignment_mode") or body.get("mode")
        try:
            if mode in {"linked", "copied", "override"}:
                profile_id = body.get("profile_id")
                if not profile_id:
                    return jsonify({"detail": "profile_id is required for profile assignment"}), 422
                updated = service.cell_config.set_assignment(
                    node_id,
                    mode,
                    profile_id=str(profile_id),
                    voc_policy_override=body.get("voc_policy_override"),
                )
            elif mode == "category":
                category_id = body.get("category_id")
                if not category_id:
                    return jsonify({"detail": "category_id is required when mode is 'category'"}), 422
                updated = service.cell_config.set_category(
                    node_id, str(category_id), body.get("voc_policy")
                )
            elif mode == "custom":
                custom_bounds = body.get("custom_bounds")
                if not isinstance(custom_bounds, dict):
                    return jsonify({"detail": "custom_bounds object is required when mode is 'custom'"}), 422
                updated = service.cell_config.set_custom_bounds(
                    node_id, custom_bounds, body.get("voc_policy")
                )
            elif mode is None and isinstance(body.get("voc_policy"), dict):
                updated = service.cell_config.set_voc_policy(node_id, body["voc_policy"])
            else:
                return jsonify(
                    {
                        "detail": (
                            "assignment_mode must be linked, copied, override, or custom; "
                            "legacy category/custom and voc_policy-only requests remain supported"
                        )
                    }
                ), 422
        except CellConfigError as exc:
            return jsonify({"detail": str(exc)}), 422

        return jsonify(updated)

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

    @app.get("/api/voc-alerts")
    def voc_alerts() -> Response:
        return jsonify({"alerts": service.storage.get_voc_alert_states()})

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


def _zone_fields(node_id: int, service: BackendService, metric_values: dict[str, Any]) -> dict[str, Any]:
    bounds = service.cell_config.get_effective_bounds(node_id)
    fields: dict[str, Any] = {}
    for metric, tile_field in (("temperature", "temperature"), ("humidity", "humidity"), ("gas", "gas")):
        zone = classify_zone(metric_values.get(tile_field), bounds.get(metric) if bounds else None)
        fields[f"{tile_field}_zone"] = zone
        fields[f"{tile_field}_bar_class"] = zone_bar_class(zone)
        fields[f"{tile_field}_badge_class"] = zone_badge_class(zone)
    return fields


def _map_node_reading(node: dict[str, Any], service: BackendService) -> dict[str, Any]:
    node_id = node.get("node_id")
    reading = {
        "protocol_version": node.get("protocol_version"),
        "gas": node.get("voc_index"),
        "temperature": node.get("temperature_c"),
        "humidity": node.get("humidity_rh"),
        "proximity": node.get("proximity"),
        "cell_empty": node.get("cell_empty"),
        "cell_status": node.get("cell_status"),
        "voc_change_score": node.get("voc_change_score"),
        "autonomous_state": node.get("autonomous_state"),
        "baseline_valid": node.get("baseline_valid"),
        "environment_fault": node.get("environment_fault"),
        "voc_6sigma": node.get("voc_6sigma"),
        "voc_10sigma": node.get("voc_10sigma"),
        "measurement_usable": node.get("measurement_usable"),
        "phase_remaining_sec": node.get("phase_remaining_sec"),
        "concurrent_state": node.get("concurrent_state"),
        "concurrent_remaining_sec": node.get("concurrent_remaining_sec"),
        "voc_decision_state": node.get("voc_decision_state"),
        "voc_warning_elapsed_seconds": node.get("voc_warning_elapsed_seconds"),
        "voc_alert_elapsed_seconds": node.get("voc_alert_elapsed_seconds"),
        "voc_alert_latched": node.get("voc_alert_latched"),
        "evaluation_state": node.get("evaluation_state"),
        "active_timer": node.get("active_timer"),
        "elapsed_seconds": node.get("elapsed_seconds"),
        "required_seconds": node.get("required_seconds"),
        "progress": node.get("progress"),
        "requirement": node.get("requirement"),
        "effective_voc_policy": node.get("effective_voc_policy"),
        "assignment_mode": node.get("assignment_mode"),
        "profile_id": node.get("profile_id"),
        "profile_name": node.get("profile_name"),
        "profile_source": node.get("profile_source"),
        "voc_policy_override": node.get("voc_policy_override"),
        "warning_active": node.get("warning_active"),
        "voc_latch_severity": node.get("voc_latch_severity"),
        "voc_critical_latched": node.get("voc_critical_latched"),
        "ts": int(node.get("received_at") or time.time()),
        "node_id": node_id,
        "address": node.get("address"),
        "name": node.get("name"),
    }
    if node_id is not None:
        reading.update(_zone_fields(int(node_id), service, reading))
    return reading


def _empty_reading(node_id: int, service: BackendService) -> dict[str, Any]:
    cell_config = service.cell_config.get_cell_config(node_id) or {}
    reading = {
        "protocol_version": None,
        "gas": None,
        "temperature": None,
        "humidity": None,
        "proximity": None,
        "cell_empty": None,
        "cell_status": None,
        "voc_change_score": None,
        "autonomous_state": None,
        "baseline_valid": False,
        "environment_fault": False,
        "measurement_usable": False,
        "phase_remaining_sec": None,
        "concurrent_state": None,
        "concurrent_remaining_sec": None,
        "voc_decision_state": "monitoring",
        "voc_warning_elapsed_seconds": 0.0,
        "voc_alert_elapsed_seconds": 0.0,
        "voc_alert_latched": False,
        "evaluation_state": "monitoring",
        "active_timer": None,
        "elapsed_seconds": 0.0,
        "required_seconds": 0,
        "progress": 0.0,
        "requirement": None,
        "effective_voc_policy": (
            cell_config.get("effective_voc_policy")
            or service.cell_config.get_effective_voc_policy(node_id)
        ),
        "assignment_mode": cell_config.get("assignment_mode"),
        "profile_id": cell_config.get("profile_id"),
        "profile_name": cell_config.get("profile_name"),
        "profile_source": cell_config.get("profile_source"),
        "voc_policy_override": cell_config.get("voc_policy_override"),
        "warning_active": False,
        "voc_latch_severity": "none",
        "voc_critical_latched": False,
        "ts": None,
        "node_id": node_id,
        "address": None,
        "name": None,
    }
    reading.update(_zone_fields(node_id, service, reading))
    return reading


def _sensor_sort_key(sensor: dict[str, Any]) -> tuple[int, int]:
    label = sensor.get("label")
    if label in SENSOR_LABEL_ORDER:
        return SENSOR_LABEL_ORDER.index(label), int(sensor.get("node_id") or 0)
    return len(SENSOR_LABEL_ORDER), int(sensor.get("node_id") or 0)


def _build_sensor(node_id: int, service: BackendService) -> dict[str, Any]:
    label = PROJECT_NODE_IDS.get(node_id)
    if label is None:
        label = f"Unmapped #{node_id}"
        name = label
        is_expected = False
        cell_config = None
    else:
        name = label
        is_expected = True
        cell_config = service.cell_config.get_cell_config(node_id)
    return {
        "id": f"node-{node_id}",
        "name": name,
        "label": label,
        "node_id": node_id,
        "is_expected": is_expected,
        "mode": cell_config["mode"] if cell_config else None,
        "assignment_label": (
            (cell_config.get("profile_name") if cell_config else None)
            or (cell_config.get("category_name") if cell_config else None)
            or "Custom"
        ),
    }


def _default_snapshot(service: BackendService) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    sensors = [_build_sensor(node_id, service) for node_id in PROJECT_NODE_IDS]
    readings = {f"node-{node_id}": _empty_reading(node_id, service) for node_id in PROJECT_NODE_IDS}
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
        readings[sensor_id] = _map_node_reading(node, service)

    expected_node_ids = set(PROJECT_NODE_IDS.keys())
    sensors = [_build_sensor(node_id, service) for node_id in expected_node_ids | seen_node_ids]
    for node_id in expected_node_ids:
        readings.setdefault(f"node-{node_id}", _empty_reading(node_id, service))

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


