#!/usr/bin/env python3
"""
Monitor SmartShelf ESP32 BLE advertisements from Raspberry Pi 5.

Expected ESP payload in manufacturer data:
  protocolVersion : uint8
  nodeId          : uint16 (or uint8 in older firmware)
  flags           : uint8
  temperature     : int16  (centi-degC)
  humidity        : uint16 (centi-%RH)
  proximityRaw    : uint16
  ambientLightRaw : uint16
  vocIndex        : int16
  noxIndex        : int16
  sequence        : uint16
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import struct
import time
from dataclasses import dataclass
from typing import Dict, Optional

from bleak import BleakScanner


DEFAULT_COMPANY_ID = 0xFFFF
EXPECTED_NODES = 9
STALE_SECONDS = 4.0
PRINT_PERIOD_SECONDS = 1.0


@dataclass
class SensorPacket:
    protocol_version: int
    node_id: int
    flags: int
    temperature_c: Optional[float]
    humidity_rh: Optional[float]
    proximity: Optional[int]
    ambient_light: Optional[int]
    voc_index: Optional[int]
    nox_index: Optional[int]
    sequence: int


@dataclass
class NodeState:
    address: str
    name: str
    packet: SensorPacket
    last_seen: float
    received_count: int
    dropped_sequences: int


def decode_payload(payload: bytes) -> SensorPacket:
    """
    Decode BLE manufacturer payload.

    Supports:
      - New payload: <B H B h H H H h h H> (18 bytes)
      - Legacy payload: <B B B h H H H h h H> (17 bytes)
    """
    if len(payload) == 18:
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
    else:
        raise ValueError(f"Unsupported payload size: {len(payload)}")

    has_sht = bool(flags & 0x01)
    has_vcnl = bool(flags & 0x02)
    has_sgp = bool(flags & 0x04)

    temperature_c = (temp_centi / 100.0) if has_sht and temp_centi != -32768 else None
    humidity_rh = (hum_centi / 100.0) if has_sht else None
    proximity_out = proximity if has_vcnl else None
    ambient_out = ambient_light if has_vcnl else None
    voc_out = voc_index if has_sgp and voc_index >= 0 else None
    nox_out = nox_index if has_sgp and nox_index >= 0 else None

    return SensorPacket(
        protocol_version=protocol_version,
        node_id=node_id,
        flags=flags,
        temperature_c=temperature_c,
        humidity_rh=humidity_rh,
        proximity=proximity_out,
        ambient_light=ambient_out,
        voc_index=voc_out,
        nox_index=nox_out,
        sequence=sequence,
    )


def seq_delta(prev: int, cur: int) -> int:
    return (cur - prev) & 0xFFFF


def clear_terminal() -> None:
    print("\033[2J\033[H", end="")


def format_optional(v: Optional[float], precision: int = 2) -> str:
    if v is None:
        return "--"
    return f"{v:.{precision}f}"


def print_status(
    nodes: Dict[int, NodeState],
    expected_nodes: int,
    stale_seconds: float,
    company_id: int,
) -> None:
    now = time.time()
    online = 0
    stale = 0

    clear_terminal()
    print("SmartShelf BLE Monitor (Raspberry Pi 5)")
    print(
        f"Company ID: 0x{company_id:04X} | "
        f"Expected nodes: {expected_nodes} | "
        f"Seen nodes: {len(nodes)} | "
        f"Stale threshold: {stale_seconds:.1f}s"
    )
    print("-" * 125)
    print(
        "Node  State   Last(s)  Seq    Drops  Temp(C)  RH(%)  Prox   ALS    VOC   NOx  Flags  Address              Name"
    )
    print("-" * 125)

    for node_id in sorted(nodes.keys()):
        state = nodes[node_id]
        age = now - state.last_seen
        is_online = age <= stale_seconds
        online += 1 if is_online else 0
        stale += 0 if is_online else 1

        pkt = state.packet
        status = "ONLINE" if is_online else "STALE "

        print(
            f"{node_id:4d}  {status:6s}  "
            f"{age:7.2f}  "
            f"{pkt.sequence:5d}  "
            f"{state.dropped_sequences:5d}  "
            f"{format_optional(pkt.temperature_c):>7s}  "
            f"{format_optional(pkt.humidity_rh):>5s}  "
            f"{str(pkt.proximity) if pkt.proximity is not None else '--':>5s}  "
            f"{str(pkt.ambient_light) if pkt.ambient_light is not None else '--':>5s}  "
            f"{str(pkt.voc_index) if pkt.voc_index is not None else '--':>4s}  "
            f"{str(pkt.nox_index) if pkt.nox_index is not None else '--':>4s}  "
            f"0x{pkt.flags:02X}   "
            f"{state.address:18.18s}  "
            f"{state.name}"
        )

    missing = max(expected_nodes - len(nodes), 0)
    print("-" * 125)
    print(
        f"ONLINE={online}  STALE={stale}  MISSING={missing}  "
        f"TOTAL_RX={sum(state.received_count for state in nodes.values())}"
    )
    print("Ctrl+C to stop.")


async def run_monitor(company_id: int, expected_nodes: int, stale_seconds: float) -> None:
    nodes: Dict[int, NodeState] = {}
    stop_event = asyncio.Event()

    def request_stop() -> None:
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, request_stop)
        except NotImplementedError:
            # add_signal_handler can be unsupported on some platforms.
            pass

    def detection_callback(device, advertisement_data) -> None:
        mfg_data = advertisement_data.manufacturer_data or {}
        payload = mfg_data.get(company_id)
        if payload is None:
            return

        try:
            packet = decode_payload(payload)
        except Exception:
            return

        now = time.time()
        prev_state = nodes.get(packet.node_id)
        dropped = 0
        rx_count = 1
        total_drops = 0

        if prev_state is not None:
            delta = seq_delta(prev_state.packet.sequence, packet.sequence)
            dropped = max(delta - 1, 0)
            rx_count = prev_state.received_count + 1
            total_drops = prev_state.dropped_sequences + dropped

        nodes[packet.node_id] = NodeState(
            address=device.address,
            name=device.name or "",
            packet=packet,
            last_seen=now,
            received_count=rx_count,
            dropped_sequences=total_drops,
        )

    scanner = BleakScanner(detection_callback=detection_callback)
    await scanner.start()
    try:
        while not stop_event.is_set():
            print_status(
                nodes=nodes,
                expected_nodes=expected_nodes,
                stale_seconds=stale_seconds,
                company_id=company_id,
            )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=PRINT_PERIOD_SECONDS)
            except asyncio.TimeoutError:
                pass
    finally:
        await scanner.stop()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Monitor 9 ESP BLE sensor broadcasters.")
    parser.add_argument(
        "--company-id",
        type=lambda x: int(x, 0),
        default=DEFAULT_COMPANY_ID,
        help="BLE manufacturer company ID (default: 0xFFFF)",
    )
    parser.add_argument(
        "--expected-nodes",
        type=int,
        default=EXPECTED_NODES,
        help="How many ESP nodes should be online (default: 9)",
    )
    parser.add_argument(
        "--stale-seconds",
        type=float,
        default=STALE_SECONDS,
        help="Node marked stale if no packet within this interval (default: 4.0)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    asyncio.run(
        run_monitor(
            company_id=args.company_id,
            expected_nodes=args.expected_nodes,
            stale_seconds=args.stale_seconds,
        )
    )


if __name__ == "__main__":
    main()
