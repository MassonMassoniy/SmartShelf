from __future__ import annotations

from typing import Dict, List, Optional

from pydantic import BaseModel


class NodeLatestReading(BaseModel):
    protocol_version: int
    node_id: int
    flags: int
    temperature_c: Optional[float]
    humidity_rh: Optional[float]
    proximity: Optional[float]
    ambient_light: Optional[float]
    white_light: Optional[float]
    ambient_lux: Optional[float]
    voc_index: Optional[float]
    nox_index: Optional[float]
    sraw_voc: Optional[float]
    sraw_nox: Optional[float]
    sequence: int
    address: str
    name: str
    received_at: float
    dropped_since_previous: int


class NodeStatus(BaseModel):
    node_id: int
    address: str
    name: str
    last_seen: float
    last_sequence: int
    received_count: int
    dropped_sequences: int
    age_seconds: float
    is_online: bool
    flags: int


class RealtimeResponse(BaseModel):
    window_seconds: int
    nodes: Dict[int, List[NodeLatestReading]]


class HealthResponse(BaseModel):
    running: bool
    company_id: int
    expected_nodes: int
    seen_nodes: int
    collector_callbacks: int
    collector_packets: int
    decode_errors: int
    db: Optional[dict]

