from __future__ import annotations

import json
import os
import time
from collections import deque
from typing import Deque, Dict, List, Optional


class TenMinuteCache:
    def __init__(
        self,
        file_path: str,
        retention_seconds: int = 600,
        flush_interval_seconds: int = 5,
    ) -> None:
        self.file_path = file_path
        self.retention_seconds = retention_seconds
        self.flush_interval_seconds = flush_interval_seconds
        self._by_node: Dict[int, Deque[dict]] = {}
        self._last_flush_ts = 0.0

    def add(self, sample: dict) -> None:
        node_id = int(sample["node_id"])
        node_deque = self._by_node.setdefault(node_id, deque())
        node_deque.append(sample)
        self.prune(now=sample["received_at"])

    def prune(self, now: float | None = None) -> None:
        if now is None:
            now = time.time()
        cutoff = now - self.retention_seconds
        for node_id in list(self._by_node.keys()):
            node_deque = self._by_node[node_id]
            while node_deque and node_deque[0]["received_at"] < cutoff:
                node_deque.popleft()
            if not node_deque:
                del self._by_node[node_id]

    def get_window(self, window_seconds: int, max_age_seconds: Optional[float] = None) -> Dict[int, List[dict]]:
        now = time.time()
        cutoff = now - max(1, window_seconds)
        freshness_cutoff = None if max_age_seconds is None else now - max(0.0, max_age_seconds)
        out: Dict[int, List[dict]] = {}
        for node_id, node_deque in self._by_node.items():
            values = [
                item
                for item in node_deque
                if item["received_at"] >= cutoff
                and (freshness_cutoff is None or item["received_at"] >= freshness_cutoff)
            ]
            if values:
                out[node_id] = values
        return out

    def latest_by_node(self, max_age_seconds: Optional[float] = None) -> Dict[int, dict]:
        freshness_cutoff = None if max_age_seconds is None else time.time() - max(0.0, max_age_seconds)
        out: Dict[int, dict] = {}
        for node_id, node_deque in self._by_node.items():
            if node_deque:
                latest = node_deque[-1]
                if freshness_cutoff is None or latest["received_at"] >= freshness_cutoff:
                    out[node_id] = latest
        return out

    def maybe_flush(self) -> None:
        now = time.time()
        if now - self._last_flush_ts >= self.flush_interval_seconds:
            self.flush()

    def flush(self) -> None:
        self.prune()
        os.makedirs(os.path.dirname(self.file_path), exist_ok=True)
        payload = {
            "saved_at": time.time(),
            "retention_seconds": self.retention_seconds,
            "nodes": {str(node_id): list(node_deque) for node_id, node_deque in self._by_node.items()},
        }

        temp_path = f"{self.file_path}.tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, separators=(",", ":"))
        os.replace(temp_path, self.file_path)
        self._last_flush_ts = time.time()

    def load(self) -> None:
        self._by_node.clear()
        if not os.path.exists(self.file_path):
            return

        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except (OSError, ValueError, TypeError, UnicodeDecodeError):
            return

        if not isinstance(payload, dict):
            return

        nodes = payload.get("nodes", {})
        if not isinstance(nodes, dict):
            return

        now = time.time()
        cutoff = now - self.retention_seconds
        for node_id_raw, samples in nodes.items():
            if not isinstance(samples, list):
                continue
            node_id = int(node_id_raw)
            node_deque: Deque[dict] = deque()
            for sample in samples:
                if isinstance(sample, dict) and sample.get("received_at", 0) >= cutoff:
                    node_deque.append(sample)
            if node_deque:
                self._by_node[node_id] = node_deque

