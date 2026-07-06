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
    sequence: int


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
        sequence=sequence,
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
        "sequence": packet.sequence,
    }

