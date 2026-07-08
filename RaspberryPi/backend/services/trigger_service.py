from __future__ import annotations

import time
from typing import Any, Optional

from ..config.constants import TRIGGER_SUSTAIN_SECONDS
from .cell_config_service import CellConfigStore
from .email_notifier import EmailNotifier
from .zone_evaluator import Zone, classify_zone, is_degradation

METRIC_FIELD_MAP = {
    "temperature": "temperature_c",
    "humidity": "humidity_rh",
    "gas": "voc_index",
}
METRIC_DISPLAY = {
    "temperature": ("Temperature", "\u00b0C"),
    "humidity": ("Humidity", "%"),
    "gas": ("VOC index", ""),
}


def _empty_state() -> dict[str, Any]:
    return {"zone": None, "zone_since": None, "previous_zone": None, "notified": False}


class TriggerService:
    """Tracks sustained green->yellow / yellow->red transitions per cell/metric
    and fires an email alert once a degradation has persisted for
    `TRIGGER_SUSTAIN_SECONDS`. Evaluation is skipped for empty cells (no product
    to protect) and state resets whenever a reading is missing/stale or the
    zone changes (the sustain timer only counts continuous time in one zone).
    """

    def __init__(
        self,
        cell_config: CellConfigStore,
        notifier: EmailNotifier,
        sustain_seconds: float = TRIGGER_SUSTAIN_SECONDS,
    ) -> None:
        self._cell_config = cell_config
        self._notifier = notifier
        self._sustain_seconds = sustain_seconds
        self._state: dict[tuple[int, str], dict[str, Any]] = {}

    def _reset(self, state: dict[str, Any]) -> None:
        state["zone"] = None
        state["zone_since"] = None
        state["previous_zone"] = None
        state["notified"] = False

    async def tick(self, latest_nodes: list[dict[str, Any]], now: Optional[float] = None) -> None:
        if now is None:
            now = time.time()
        by_node_id = {int(node["node_id"]): node for node in latest_nodes if node.get("node_id") is not None}

        for node_id in self._cell_config.project_node_ids():
            node = by_node_id.get(node_id)
            is_empty = bool(node.get("cell_empty")) if node else False
            bounds = self._cell_config.get_effective_bounds(node_id) if node and not is_empty else None

            for metric, field in METRIC_FIELD_MAP.items():
                state = self._state.setdefault((node_id, metric), _empty_state())

                if node is None or is_empty or bounds is None:
                    self._reset(state)
                    continue

                value = node.get(field)
                zone: Optional[Zone] = classify_zone(value, bounds.get(metric))

                if zone != state["zone"]:
                    state["previous_zone"] = state["zone"]
                    state["zone"] = zone
                    state["zone_since"] = now
                    state["notified"] = False

                if (
                    zone in ("yellow", "red")
                    and not state["notified"]
                    and is_degradation(state["previous_zone"], zone)
                    and state["zone_since"] is not None
                    and (now - state["zone_since"]) >= self._sustain_seconds
                ):
                    await self._fire(node_id, metric, state, value, zone)
                    state["notified"] = True

    async def _fire(
        self,
        node_id: int,
        metric: str,
        state: dict[str, Any],
        value: Any,
        zone: Zone,
    ) -> None:
        cell_config = self._cell_config.get_cell_config(node_id)
        label = cell_config["label"] if cell_config else str(node_id)
        assignment = (cell_config or {}).get("category_name") or "Custom bounds"
        metric_label, unit = METRIC_DISPLAY[metric]
        previous_zone = state["previous_zone"] or "unknown"
        try:
            value_str = f"{float(value):.1f}{unit}"
        except (TypeError, ValueError):
            value_str = "--"

        sustain_minutes = self._sustain_seconds / 60
        await self._notifier.send_alert(
            cell_label=label,
            assignment=assignment,
            metric_label=metric_label,
            previous_zone=previous_zone,
            new_zone=zone,
            value_str=value_str,
            sustain_minutes=sustain_minutes,
        )
