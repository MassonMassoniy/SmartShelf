#!/usr/bin/env python3
"""Terminal monitor for SmartShelf BLE packets."""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
import time
from pathlib import Path
from typing import Dict, Optional

REPO_ROOT = Path(__file__).resolve().parents[3]

# Allow `python monitor_ble.py` when cwd is RaspberryPi/backend/app.
if __package__ in (None, ""):
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))

from RaspberryPi.backend.config.constants import DEFAULT_COMPANY_ID, EXPECTED_NODES, STALE_SECONDS
from RaspberryPi.backend.ingestion.collector import BlePacketCollector
from RaspberryPi.backend.ingestion.protocol_decoder import NodeState
PRINT_PERIOD_SECONDS = 1.0


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
    print("-" * 138)
    print(
        "Node  State   Last(s)  Seq    Drops  Temp(C)  RH(%)  Prox   VOC  Score  Auto  Quality       Flags  Address              Name"
    )
    print("-" * 138)

    for node_id in sorted(nodes.keys()):
        state = nodes[node_id]
        age = now - state.last_seen
        is_online = age <= stale_seconds
        online += 1 if is_online else 0
        stale += 0 if is_online else 1

        pkt = state.packet
        status = "ONLINE" if is_online else "STALE "
        quality = (
            "FAULT"
            if pkt.environment_fault
            else "USABLE"
            if pkt.measurement_usable
            else "WAIT"
            if pkt.protocol_version >= 5
            else "LEGACY"
        )

        print(
            f"{node_id:4d}  {status:6s}  "
            f"{age:7.2f}  "
            f"{pkt.sequence:5d}  "
            f"{state.dropped_sequences:5d}  "
            f"{format_optional(pkt.temperature_c):>7s}  "
            f"{format_optional(pkt.humidity_rh):>5s}  "
            f"{str(pkt.proximity) if pkt.proximity is not None else '--':>5s}  "
            f"{str(pkt.voc_index) if pkt.voc_index is not None else '--':>4s}  "
            f"{str(pkt.voc_change_score) if pkt.voc_change_score is not None else '--':>5s}  "
            f"{str(pkt.autonomous_state) if pkt.autonomous_state is not None else '--':>4s}  "
            f"{quality:>11s}  "
            f"0x{pkt.flags:02X}   "
            f"{state.address:18.18s}  "
            f"{state.name}"
        )

    missing = max(expected_nodes - len(nodes), 0)
    print("-" * 138)
    print(
        f"ONLINE={online}  STALE={stale}  MISSING={missing}  "
        f"TOTAL_RX={sum(state.received_count for state in nodes.values())}"
    )
    print("Ctrl+C to stop.")


async def run_monitor(company_id: int, expected_nodes: int, stale_seconds: float) -> None:
    collector = BlePacketCollector(company_id=company_id)
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

    await collector.start()
    try:
        while not stop_event.is_set():
            print_status(
                nodes=collector.nodes,
                expected_nodes=expected_nodes,
                stale_seconds=stale_seconds,
                company_id=company_id,
            )
            print(
                f"Callbacks={collector.total_callbacks}  "
                f"DecodedPackets={collector.total_packets}  "
                f"DecodeErrors={collector.decode_errors}"
            )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=PRINT_PERIOD_SECONDS)
            except asyncio.TimeoutError:
                pass
    finally:
        await collector.stop()


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

