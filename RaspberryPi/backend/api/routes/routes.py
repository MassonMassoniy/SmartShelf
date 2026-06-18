from __future__ import annotations

from typing import Literal, Optional

from fastapi import FastAPI, Query

from ..schemas.models import HealthResponse, NodeLatestReading, NodeStatus, RealtimeResponse
from ...services.backend_service import BackendService


def create_api(service: BackendService, lifespan=None) -> FastAPI:
    app = FastAPI(title="SmartShelf Backend", version="1.0.0", lifespan=lifespan)

    @app.get("/health", response_model=HealthResponse)
    async def health() -> dict:
        return service.get_health()

    @app.get("/nodes/latest", response_model=list[NodeLatestReading])
    async def nodes_latest() -> list[dict]:
        return service.get_latest_nodes()

    @app.get("/nodes/realtime", response_model=RealtimeResponse)
    async def nodes_realtime(window_seconds: int = Query(default=600, ge=1, le=3600)) -> dict:
        return {
            "window_seconds": window_seconds,
            "nodes": service.get_realtime_window(window_seconds=window_seconds),
        }

    @app.get("/nodes/status", response_model=list[NodeStatus])
    async def nodes_status() -> list[dict]:
        return service.get_node_status()

    @app.get("/nodes/minute-history")
    async def minute_history(
        outliers: Literal["include", "exclude"] = Query(default="include"),
        from_ts: Optional[int] = Query(default=None),
        to_ts: Optional[int] = Query(default=None),
        node_id: Optional[int] = Query(default=None),
    ) -> list[dict]:
        return service.storage.get_minute_history(
            outliers=outliers,
            from_ts=from_ts,
            to_ts=to_ts,
            node_id=node_id,
        )

    return app

