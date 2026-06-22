from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from bleak import BleakScanner

from .protocol_decoder import NodeState, SensorPacket, decode_payload, seq_delta

PacketListener = Callable[["PacketEvent"], None]


@dataclass
class PacketEvent:
    packet: SensorPacket
    address: str
    name: str
    received_at: float
    dropped_since_previous: int
    is_new_sequence: bool


class BlePacketCollector:
    def __init__(self, company_id: int) -> None:
        self.company_id = company_id
        self.nodes: Dict[int, NodeState] = {}
        self.decode_errors = 0
        self.total_callbacks = 0
        self.total_packets = 0
        self._listeners: List[PacketListener] = []
        self._scanner: Optional[BleakScanner] = None

    def add_listener(self, listener: PacketListener) -> None:
        self._listeners.append(listener)

    def remove_listener(self, listener: PacketListener) -> None:
        self._listeners = [cb for cb in self._listeners if cb is not listener]

    def on_advertisement(self, device, advertisement_data) -> None:
        self.total_callbacks += 1
        mfg_data = advertisement_data.manufacturer_data or {}
        payload = mfg_data.get(self.company_id)
        if payload is None:
            return

        try:
            packet = decode_payload(payload)
        except Exception:
            self.decode_errors += 1
            return

        self.total_packets += 1
        now = time.time()
        prev_state = self.nodes.get(packet.node_id)
        dropped = 0
        rx_count = 1
        total_drops = 0
        is_new_sequence = True

        if prev_state is not None:
            delta = seq_delta(prev_state.packet.sequence, packet.sequence)
            dropped = max(delta - 1, 0)
            rx_count = prev_state.received_count + 1
            total_drops = prev_state.dropped_sequences + dropped
            is_new_sequence = delta > 0

        self.nodes[packet.node_id] = NodeState(
            address=device.address,
            name=device.name or "",
            packet=packet,
            last_seen=now,
            received_count=rx_count,
            dropped_sequences=total_drops,
        )

        event = PacketEvent(
            packet=packet,
            address=device.address,
            name=device.name or "",
            received_at=now,
            dropped_since_previous=dropped,
            is_new_sequence=is_new_sequence,
        )
        for callback in self._listeners:
            callback(event)

    async def start(self) -> None:
        if self._scanner is not None:
            return
        self._scanner = BleakScanner(detection_callback=self.on_advertisement)
        await self._scanner.start()

    async def stop(self) -> None:
        if self._scanner is None:
            return
        await self._scanner.stop()
        self._scanner = None

