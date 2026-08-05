from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Optional

from ..config.constants import DEFAULT_COMPANY_ID, EXPECTED_NODES, STALE_SECONDS

@dataclass
class SensorPacket:
    protocol_version: int
    node_id: int
    flags: int
    temperature_c: Optional[float]
    humidity_rh: Optional[float]
    proximity: Optional[int]
    ambient_light: Optional[int]
    white_light: Optional[int]
    ambient_lux: Optional[float]
    voc_index: Optional[int]
    nox_index: Optional[int]
    sraw_voc: Optional[int]
    sraw_nox: Optional[int]
    voc_change_score: Optional[int]
    autonomous_state: Optional[int]
    baseline_valid: bool
    environment_fault: bool
    voc_6sigma: bool
    voc_10sigma: bool
    measurement_usable: bool
    sequence: int
    phase_remaining_sec: Optional[int] = None
    concurrent_state: Optional[int] = None
    concurrent_remaining_sec: Optional[int] = None


def is_autonomous_protocol(protocol_version: int) -> bool:
    return protocol_version >= 5


@dataclass
class NodeState:
    address: str
    name: str
    packet: SensorPacket
    last_seen: float
    received_count: int
    dropped_sequences: int


def seq_delta(prev: int, cur: int) -> int:
    return (cur - prev) & 0xFFFF


def decode_payload(payload: bytes) -> SensorPacket:
    """Decode SmartShelf BLE manufacturer payload."""
    ambient_light = None
    white_light = None
    ambient_deci_lux = 0
    sraw_voc = 0
    sraw_nox = 0
    nox_index = None
    voc_change_score = None
    autonomous_state = None
    phase_remaining_sec = None
    concurrent_state = None
    concurrent_remaining_sec = None

    if len(payload) == 14:
        (
            protocol_version,
            node_id,
            flags,
            temp_centi,
            hum_centi,
            proximity,
            voc_index,
            sequence,
        ) = struct.unpack("<BHBhHHhH", payload)
    elif len(payload) == 21 and payload[0] >= 6:
        (
            protocol_version,
            node_id,
            flags,
            temp_centi,
            hum_centi,
            proximity,
            voc_index,
            score_raw,
            autonomous_state,
            sequence,
            phase_remaining_sec,
            concurrent_state,
            concurrent_remaining_sec,
        ) = struct.unpack("<BHBhHHhBBHHBH", payload)
        voc_change_score = score_raw if 0 <= score_raw <= 100 else None
        if concurrent_state == 0:
            concurrent_state = None
            concurrent_remaining_sec = None
    elif len(payload) == 16 and payload[0] == 5:
        (
            protocol_version,
            node_id,
            flags,
            temp_centi,
            hum_centi,
            proximity,
            voc_index,
            score_raw,
            autonomous_state,
            sequence,
        ) = struct.unpack("<BHBhHHhBBH", payload)
        voc_change_score = score_raw if 0 <= score_raw <= 100 else None
    elif len(payload) == 16:
        (
            protocol_version,
            node_id,
            flags,
            temp_centi,
            hum_centi,
            proximity,
            voc_index,
            nox_index,
            sequence,
        ) = struct.unpack("<BHBhHHhhH", payload)
    elif len(payload) == 26:
        (
            protocol_version,
            node_id,
            flags,
            temp_centi,
            hum_centi,
            proximity,
            ambient_light,
            white_light,
            ambient_deci_lux,
            voc_index,
            nox_index,
            sraw_voc,
            sraw_nox,
            sequence,
        ) = struct.unpack("<BHBhHHHHHhhHHH", payload)
    elif len(payload) == 24:
        (
            protocol_version,
            node_id,
            flags,
            temp_centi,
            hum_centi,
            proximity,
            white_light,
            ambient_deci_lux,
            voc_index,
            nox_index,
            sraw_voc,
            sraw_nox,
            sequence,
        ) = struct.unpack("<BHBhHHHHhhHHH", payload)
        ambient_light = None
    elif len(payload) == 18:
        (
            protocol_version,
            node_id,
            flags,
            temp_centi,
            hum_centi,
            proximity,
            ambient_light,
            voc_index,
            nox_index,
            sequence,
        ) = struct.unpack("<BHBhHHHhhH", payload)
        white_light = 0
        ambient_deci_lux = 0
    elif len(payload) == 17:
        (
            protocol_version,
            node_id,
            flags,
            temp_centi,
            hum_centi,
            proximity,
            ambient_light,
            voc_index,
            nox_index,
            sequence,
        ) = struct.unpack("<BBBhHHHhhH", payload)
        white_light = 0
        ambient_deci_lux = 0
    else:
        raise ValueError(f"Unsupported payload size: {len(payload)}")

    has_sht = bool(flags & 0x01)
    has_vcnl = bool(flags & 0x02)
    has_sgp = bool(flags & 0x04)
    autonomous = is_autonomous_protocol(protocol_version)
    baseline_valid = autonomous and bool(flags & 0x08)
    environment_fault = autonomous and (
        bool(flags & 0x10) or autonomous_state == 8
    )
    voc_6sigma = autonomous and bool(flags & 0x20)
    voc_10sigma = autonomous and bool(flags & 0x40)
    measurement_usable = bool(
        autonomous
        and has_sgp
        and baseline_valid
        and not environment_fault
        and voc_change_score is not None
        and autonomous_state == 5
    )

    temperature_c = (temp_centi / 100.0) if has_sht and temp_centi != -32768 else None
    humidity_rh = (hum_centi / 100.0) if has_sht else None
    proximity_out = proximity if has_vcnl else None
    ambient_out = ambient_light if has_vcnl else None
    white_out = white_light if has_vcnl else None
    lux_out = (ambient_deci_lux / 10.0) if has_vcnl and ambient_deci_lux > 0 else None
    voc_out = voc_index if has_sgp and voc_index >= 0 else None
    nox_out = nox_index if has_sgp and nox_index is not None and nox_index >= 0 else None
    sraw_voc_out = sraw_voc if has_sgp and sraw_voc > 0 else None
    sraw_nox_out = sraw_nox if has_sgp and sraw_nox > 0 else None

    return SensorPacket(
        protocol_version=protocol_version,
        node_id=node_id,
        flags=flags,
        temperature_c=temperature_c,
        humidity_rh=humidity_rh,
        proximity=proximity_out,
        ambient_light=ambient_out,
        white_light=white_out,
        ambient_lux=lux_out,
        voc_index=voc_out,
        nox_index=nox_out,
        sraw_voc=sraw_voc_out,
        sraw_nox=sraw_nox_out,
        voc_change_score=voc_change_score,
        autonomous_state=autonomous_state,
        baseline_valid=baseline_valid,
        environment_fault=environment_fault,
        voc_6sigma=voc_6sigma,
        voc_10sigma=voc_10sigma,
        measurement_usable=measurement_usable,
        sequence=sequence,
        phase_remaining_sec=phase_remaining_sec,
        concurrent_state=concurrent_state,
        concurrent_remaining_sec=concurrent_remaining_sec,
    )


def packet_to_dict(packet: SensorPacket) -> dict:
    return {
        "protocol_version": packet.protocol_version,
        "node_id": packet.node_id,
        "flags": packet.flags,
        "temperature_c": packet.temperature_c,
        "humidity_rh": packet.humidity_rh,
        "proximity": packet.proximity,
        "ambient_light": packet.ambient_light,
        "white_light": packet.white_light,
        "ambient_lux": packet.ambient_lux,
        "voc_index": packet.voc_index,
        "nox_index": packet.nox_index,
        "sraw_voc": packet.sraw_voc,
        "sraw_nox": packet.sraw_nox,
        "voc_change_score": packet.voc_change_score,
        "autonomous_state": packet.autonomous_state,
        "baseline_valid": packet.baseline_valid,
        "environment_fault": packet.environment_fault,
        "voc_6sigma": packet.voc_6sigma,
        "voc_10sigma": packet.voc_10sigma,
        "measurement_usable": packet.measurement_usable,
        "sequence": packet.sequence,
        "phase_remaining_sec": packet.phase_remaining_sec,
        "concurrent_state": packet.concurrent_state,
        "concurrent_remaining_sec": packet.concurrent_remaining_sec,
    }

