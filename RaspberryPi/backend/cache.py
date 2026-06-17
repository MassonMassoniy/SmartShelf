from __future__ import annotations

import json
import os
import time
from collections import deque
from typing import Deque, Dict, List


class RollingSecondCache:
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

    def get_window(self, window_seconds: int) -> Dict[int, List[dict]]:
        now = time.time()
        cutoff = now - max(1, window_seconds)
        out: Dict[int, List[dict]] = {}
        for node_id, node_deque in self._by_node.items():
            values = [item for item in node_deque if item["received_at"] >= cutoff]
            if values:
                out[node_id] = values
        return out

    def latest_by_node(self) -> Dict[int, dict]:
        out: Dict[int, dict] = {}
        for node_id, node_deque in self._by_node.items():
            if node_deque:
                out[node_id] = node_deque[-1]
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
        if not os.path.exists(self.file_path):
            return
        with open(self.file_path, "r", encoding="utf-8") as f:
            payload = json.load(f)

        nodes = payload.get("nodes", {})
        self._by_node.clear()
        now = time.time()
        cutoff = now - self.retention_seconds
        for node_id_raw, samples in nodes.items():
            node_id = int(node_id_raw)
            node_deque: Deque[dict] = deque()
            for sample in samples:
                if sample.get("received_at", 0) >= cutoff:
                    node_deque.append(sample)
            if node_deque:
                self._by_node[node_id] = node_deque

