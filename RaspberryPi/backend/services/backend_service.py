from __future__ import annotations

import asyncio
import math
import time
from collections import defaultdict
from typing import Dict, List, Optional

from ..storage.cache.ten_minute_cache import TenMinuteCache
from ..ingestion.collector import BlePacketCollector, PacketEvent
from ..ingestion.protocol_decoder import STALE_SECONDS, packet_to_dict
from ..storage.sqlite.sqlite_storage import NO_OUTLIERS_TABLE, WITH_OUTLIERS_TABLE, SQLiteStorage


METRIC_FIELDS = [
    "temperature_c",
    "humidity_rh",
    "proximity",
    "ambient_light",
    "white_light",
    "ambient_lux",
    "voc_index",
    "nox_index",
    "sraw_voc",
    "sraw_nox",
]


def minute_bucket(ts: float) -> int:
    return int(ts // 60) * 60


def _compute_mean(values: List[float]) -> Optional[float]:
    if not values:
        return None
    return float(sum(values) / len(values))


def _percentile(sorted_values: List[float], p: float) -> float:
    if not sorted_values:
        raise ValueError("percentile requires non-empty list")
    idx = (len(sorted_values) - 1) * p
    lower = math.floor(idx)
    upper = math.ceil(idx)
    if lower == upper:
        return sorted_values[int(idx)]
    frac = idx - lower
    return sorted_values[lower] * (1 - frac) + sorted_values[upper] * frac


def _filter_iqr(values: List[float]) -> List[float]:
    if len(values) < 4:
        return values
    ordered = sorted(values)
    q1 = _percentile(ordered, 0.25)
    q3 = _percentile(ordered, 0.75)
    iqr = q3 - q1
    if iqr == 0:
        return values
    low = q1 - 1.5 * iqr
    high = q3 + 1.5 * iqr
    filtered = [v for v in values if low <= v <= high]
    return filtered or values


class BackendService:
    def __init__(
        self,
        company_id: int,
        db_path: str,
        cache_path: str,
        expected_nodes: int,
        stale_seconds: float = STALE_SECONDS,
    ) -> None:
        self.company_id = company_id
        self.expected_nodes = expected_nodes
        self.stale_seconds = stale_seconds

        self.collector = BlePacketCollector(company_id=company_id)
        self.cache = TenMinuteCache(file_path=cache_path)
        self.storage = SQLiteStorage(db_path=db_path)

        self.decode_errors = 0
        self._minute_samples: Dict[tuple[int, int], List[dict]] = defaultdict(list)
        self._persist_task: Optional[asyncio.Task] = None
        self._running = False

        self.collector.add_listener(self._on_packet)

    def _on_packet(self, event: PacketEvent) -> None:
        self.decode_errors = self.collector.decode_errors
        if not event.is_new_sequence:
            return

        sample = packet_to_dict(event.packet)
        sample["address"] = event.address
        sample["name"] = event.name
        sample["received_at"] = event.received_at
        sample["dropped_since_previous"] = event.dropped_since_previous

        self.cache.add(sample)
        bucket = minute_bucket(event.received_at)
        key = (bucket, event.packet.node_id)
        self._minute_samples[key].append(sample)

        node_state = self.collector.nodes[event.packet.node_id]
        self.storage.upsert_node_health(
            node_id=event.packet.node_id,
            last_seen=node_state.last_seen,
            last_sequence=node_state.packet.sequence,
            dropped_sequences_total=node_state.dropped_sequences,
            address=node_state.address,
            name=node_state.name,
        )

    async def _periodic_maintenance(self) -> None:
        while self._running:
            self.flush_finished_minutes()
            self.cache.maybe_flush()
            await asyncio.sleep(1)

    def _build_aggregate_row(
        self,
        minute_ts: int,
        node_id: int,
        samples: List[dict],
        use_outlier_filter: bool,
    ) -> dict:
        latest = max(samples, key=lambda s: s["received_at"])
        row = {
            "minute_ts": minute_ts,
            "node_id": node_id,
            "protocol_version": latest["protocol_version"],
            "flags": latest["flags"],
            "sample_count": len(samples),
            "computed_at": time.time(),
        }

        for field in METRIC_FIELDS:
            values = [float(s[field]) for s in samples if s.get(field) is not None]
            if use_outlier_filter:
                values = _filter_iqr(values)
            row[field] = _compute_mean(values)
        return row

    def flush_finished_minutes(self) -> None:
        current_bucket = minute_bucket(time.time())
        finished_keys = [key for key in self._minute_samples.keys() if key[0] < current_bucket]
        for minute_ts, node_id in finished_keys:
            samples = self._minute_samples.pop((minute_ts, node_id))
            if not samples:
                continue
            include_row = self._build_aggregate_row(
                minute_ts=minute_ts,
                node_id=node_id,
                samples=samples,
                use_outlier_filter=False,
            )
            exclude_row = self._build_aggregate_row(
                minute_ts=minute_ts,
                node_id=node_id,
                samples=samples,
                use_outlier_filter=True,
            )
            self.storage.upsert_minute_average(WITH_OUTLIERS_TABLE, include_row)
            self.storage.upsert_minute_average(NO_OUTLIERS_TABLE, exclude_row)

    async def start(self) -> None:
        if self._running:
            return
        self.storage.start()
        self.cache.load()
        await self.collector.start()
        self._running = True
        self._persist_task = asyncio.create_task(self._periodic_maintenance())

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        if self._persist_task is not None:
            self._persist_task.cancel()
            try:
                await self._persist_task
            except asyncio.CancelledError:
                pass
            self._persist_task = None
        self.flush_finished_minutes()
        self.cache.flush()
        await self.collector.stop()
        self.storage.stop()

    def get_latest_nodes(self) -> List[dict]:
        latest = self.cache.latest_by_node(max_age_seconds=self.stale_seconds)
        return [latest[node_id] for node_id in sorted(latest.keys())]

    def get_realtime_window(self, window_seconds: int) -> Dict[int, List[dict]]:
        return self.cache.get_window(window_seconds=window_seconds)

    def get_node_status(self) -> List[dict]:
        now = time.time()
        status = []
        for node_id in sorted(self.collector.nodes.keys()):
            state = self.collector.nodes[node_id]
            age = now - state.last_seen
            status.append(
                {
                    "node_id": node_id,
                    "address": state.address,
                    "name": state.name,
                    "last_seen": state.last_seen,
                    "last_sequence": state.packet.sequence,
                    "received_count": state.received_count,
                    "dropped_sequences": state.dropped_sequences,
                    "age_seconds": age,
                    "is_online": age <= self.stale_seconds,
                    "flags": state.packet.flags,
                }
            )
        return status

    def get_health(self) -> dict:
        return {
            "running": self._running,
            "company_id": self.company_id,
            "expected_nodes": self.expected_nodes,
            "seen_nodes": len(self.collector.nodes),
            "collector_callbacks": self.collector.total_callbacks,
            "collector_packets": self.collector.total_packets,
            "decode_errors": self.collector.decode_errors,
            "db": self.storage.health_summary() if self.storage.conn is not None else None,
        }

