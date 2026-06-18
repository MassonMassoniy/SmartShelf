from __future__ import annotations

import argparse
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from .api import create_api
from .protocol import DEFAULT_COMPANY_ID, EXPECTED_NODES, STALE_SECONDS
from .service import BackendService


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


def build_app(args: argparse.Namespace) -> FastAPI:
    service = BackendService(
        company_id=args.company_id,
        db_path=args.db_path,
        cache_path=args.cache_path,
        expected_nodes=args.expected_nodes,
        stale_seconds=args.stale_seconds,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await service.start()
        try:
            yield
        finally:
            await service.stop()

    return create_api(service, lifespan=lifespan)


def main() -> None:
    args = parse_args()
    app = build_app(args)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()

