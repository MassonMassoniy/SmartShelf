from __future__ import annotations

import argparse
import atexit
import asyncio
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, render_template, request, stream_with_context

REPO_ROOT = Path(__file__).resolve().parents[3]

# Allow `python app/main.py` when cwd is RaspberryPi/backend.
if __package__ in (None, ""):
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))

from RaspberryPi.backend.config.constants import DEFAULT_COMPANY_ID, EXPECTED_NODES, STALE_SECONDS
from RaspberryPi.backend.services.backend_service import BackendService


class AsyncServiceRuntime:
    def __init__(self, service: BackendService) -> None:
        self.service = service
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def _run_loop(self) -> None:
        assert self._loop is not None
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

        # Ensure pending tasks are cancelled before closing the loop.
        pending = asyncio.all_tasks(self._loop)
        for task in pending:
            task.cancel()
        if pending:
            self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        self._loop.close()

    def start(self) -> None:
        with self._lock:
            if self._loop is not None:
                return

            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(target=self._run_loop, name="smartshelf-service-loop", daemon=True)
            self._thread.start()

            future = asyncio.run_coroutine_threadsafe(self.service.start(), self._loop)
            future.result(timeout=15)

    def stop(self) -> None:
        with self._lock:
            if self._loop is None:
                return

            loop = self._loop
            thread = self._thread

            try:
                future = asyncio.run_coroutine_threadsafe(self.service.stop(), loop)
                future.result(timeout=15)
            finally:
                loop.call_soon_threadsafe(loop.stop)
                if thread is not None:
                    thread.join(timeout=5)
                self._loop = None
                self._thread = None


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

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SmartShelf Raspberry Pi backend service.")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host for HTTP server")
    parser.add_argument("--port", type=int, default=8000, help="Bind port for HTTP server")
    parser.add_argument("--company-id", type=lambda x: int(x, 0), default=DEFAULT_COMPANY_ID)
    parser.add_argument("--expected-nodes", type=int, default=EXPECTED_NODES)
    parser.add_argument("--stale-seconds", type=float, default=STALE_SECONDS)
    parser.add_argument("--db-path", default="RaspberryPi/data/smartshelf.db")
    parser.add_argument("--cache-path", default="RaspberryPi/data/second_cache.json")
    return parser.parse_args()


def build_app(args: argparse.Namespace) -> Flask:
    service = BackendService(
        company_id=args.company_id,
        db_path=args.db_path,
        cache_path=args.cache_path,
        expected_nodes=args.expected_nodes,
        stale_seconds=args.stale_seconds,
    )

    runtime = AsyncServiceRuntime(service)

    template_dir = REPO_ROOT / "RaspberryPi" / "frontend" / "templates"
    app = Flask(__name__, template_folder=str(template_dir))
    app.config["SMARTSHELF_SERVICE"] = service
    app.config["SMARTSHELF_RUNTIME"] = runtime
    app.config["JSON_SORT_KEYS"] = False

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

    return app


def main_back() -> None:
    args = parse_args()
    app = build_app(args)
    runtime: AsyncServiceRuntime = app.config["SMARTSHELF_RUNTIME"]

    runtime.start()
    atexit.register(runtime.stop)

    try:
        app.run(host=args.host, port=args.port, threaded=True)
    finally:
        runtime.stop()


if __name__ == "__main__":
    main_back()

