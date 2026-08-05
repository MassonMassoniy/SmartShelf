from __future__ import annotations

from typing import Any, Dict, List, Optional

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
    voc_change_score: Optional[float] = None
    autonomous_state: Optional[int] = None
    baseline_valid: bool = False
    environment_fault: bool = False
    voc_6sigma: bool = False
    voc_10sigma: bool = False
    measurement_usable: bool = False
    voc_decision_state: str = "monitoring"
    voc_warning_elapsed_seconds: float = 0.0
    voc_alert_elapsed_seconds: float = 0.0
    voc_alert_latched: bool = False
    evaluation_state: str = "monitoring"
    active_timer: Optional[str] = None
    elapsed_seconds: float = 0.0
    required_seconds: int = 0
    progress: float = 0.0
    requirement: Optional[dict] = None
    effective_voc_policy: Optional[Dict[str, Any]] = None
    assignment_mode: Optional[str] = None
    profile_id: Optional[str] = None
    profile_name: Optional[str] = None
    profile_source: Optional[str] = None
    voc_policy_override: Optional[Dict[str, Any]] = None
    warning_active: bool = False
    voc_latch_severity: str = "none"
    voc_critical_latched: bool = False
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

