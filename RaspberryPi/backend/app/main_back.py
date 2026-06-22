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
from RaspberryPi.backend.api.routes.routes import create_api
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

    create_api(service, app)

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

