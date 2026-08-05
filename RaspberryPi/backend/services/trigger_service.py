from __future__ import annotations

import time
from typing import Any, Optional

from ..config.constants import TRIGGER_SUSTAIN_SECONDS
from .cell_config_service import CellConfigStore
from .email_notifier import EmailNotifier
from .zone_evaluator import Zone, classify_zone, is_degradation
from ..storage.sqlite.sqlite_storage import SQLiteStorage

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
    `TRIGGER_SUSTAIN_SECONDS`. Alerts require an observed previous zone of green
    or yellow (cold-start / unknown readings do not notify). Evaluation is
    skipped for empty cells (no product to protect) and state resets whenever a
    reading is missing/stale or the zone changes (the sustain timer only counts
    continuous time in one zone).
    """

    def __init__(
        self,
        cell_config: CellConfigStore,
        notifier: EmailNotifier,
        sustain_seconds: float = TRIGGER_SUSTAIN_SECONDS,
        storage: Optional[SQLiteStorage] = None,
    ) -> None:
        self._cell_config = cell_config
        self._notifier = notifier
        self._sustain_seconds = sustain_seconds
        self._storage = storage or cell_config._storage
        self._state: dict[tuple[int, str], dict[str, Any]] = {}
        self._voc_state: dict[int, dict[str, Any]] = {}

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

                # Protocol v5 replaces only the legacy VOC Index trigger. Its
                # temperature and humidity zone triggers remain unchanged.
                if metric == "gas" and node is not None and int(node.get("protocol_version") or 0) >= 5:
                    self._reset(state)
                    continue
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

                previous_zone = state["previous_zone"]
                if (
                    zone in ("yellow", "red")
                    and previous_zone in ("green", "yellow")
                    and not state["notified"]
                    and is_degradation(previous_zone, zone)
                    and state["zone_since"] is not None
                    and (now - state["zone_since"]) >= self._sustain_seconds
                ):
                    await self._fire(node_id, metric, state, value, zone)
                    state["notified"] = True

            await self._tick_autonomous_voc(node_id, node, now)

    async def _tick_autonomous_voc(
        self, node_id: int, node: Optional[dict[str, Any]], now: float
    ) -> None:
        state = self._get_or_restore_voc_state(node_id)

        # A v5 EMPTY state is the sole acknowledgement that clears a durable
        # alert. Score drops, faults, stale data, and sequence restarts do not.
        if (
            node is not None
            and int(node.get("protocol_version") or 0) >= 5
            and node.get("autonomous_state") == 2
        ):
            self._reset_voc_pending(state)
            if state["warning_active"] or state["latch_severity"] != "none":
                state.update(warning_active=False, latch_severity="none")
                self._storage.set_voc_state(node_id, False, "none", now)
            return

        if (
            node is None
            or int(node.get("protocol_version") or 0) < 5
            or not node.get("measurement_usable")
        ):
            self._reset_voc_pending(state)
            return

        policy = self._cell_config.get_effective_voc_policy(node_id)
        score = node.get("voc_change_score")
        if policy is None or score is None:
            self._reset_voc_pending(state)
            return

        warning_true = int(score) >= policy["warning_score"] and self._sigma_set(
            node, policy["warning_sigma"]
        )
        alert_true = int(score) >= policy["alert_score"] and self._sigma_set(
            node, policy["alert_sigma"]
        )

        critical_enabled = policy["critical_score"] is not None
        critical_true = (
            critical_enabled
            and int(score) >= policy["critical_score"]
            and self._sigma_set(node, policy["critical_sigma"])
        )

        if state["warning_active"]:
            state["warning_since"] = None
            if int(score) <= policy["warning_clear_score"]:
                if state["warning_clear_since"] is None:
                    state["warning_clear_since"] = now
                if now - state["warning_clear_since"] >= policy["warning_clear_seconds"]:
                    state["warning_active"] = False
                    state["warning_clear_since"] = None
                    self._storage.set_voc_state(
                        node_id, False, state["latch_severity"], now
                    )
            else:
                state["warning_clear_since"] = None
        elif warning_true:
            state["warning_clear_since"] = None
            if state["warning_since"] is None:
                state["warning_since"] = now
            if now - state["warning_since"] >= policy["warning_seconds"]:
                state["warning_active"] = True
                state["warning_since"] = None
                self._storage.set_voc_state(
                    node_id, True, state["latch_severity"], now
                )
                await self._fire_voc(node_id, "warning", score, policy)
        else:
            state["warning_since"] = None

        if state["latch_severity"] == "none" and alert_true:
            if state["alert_since"] is None:
                state["alert_since"] = now
            if now - state["alert_since"] >= policy["alert_seconds"]:
                state["latch_severity"] = "alert"
                self._storage.set_voc_state(
                    node_id, state["warning_active"], "alert", now
                )
                await self._fire_voc(node_id, "alert", score, policy)
                state["alert_since"] = None
        else:
            state["alert_since"] = None

        if state["latch_severity"] != "critical" and critical_true:
            if state["critical_since"] is None:
                state["critical_since"] = now
            if now - state["critical_since"] >= policy["critical_seconds"]:
                state["latch_severity"] = "critical"
                self._storage.set_voc_state(
                    node_id, state["warning_active"], "critical", now
                )
                await self._fire_voc(node_id, "critical", score, policy)
                state["critical_since"] = None
        else:
            state["critical_since"] = None

    def _get_or_restore_voc_state(self, node_id: int) -> dict[str, Any]:
        state = self._voc_state.get(node_id)
        if state is not None:
            return state
        durable = self._storage.get_voc_state(node_id)
        state = {
            "warning_since": None,
            "warning_clear_since": None,
            "alert_since": None,
            "critical_since": None,
            "warning_active": bool(durable["warning_active"]),
            "latch_severity": durable["latch_severity"],
        }
        self._voc_state[node_id] = state
        return state

    @staticmethod
    def _reset_voc_pending(state: dict[str, Any]) -> None:
        state.update(
            warning_since=None,
            warning_clear_since=None,
            alert_since=None,
            critical_since=None,
        )

    @staticmethod
    def _sigma_set(node: dict[str, Any], sigma: int) -> bool:
        return bool(node.get("voc_10sigma" if sigma == 10 else "voc_6sigma"))

    def get_voc_status(self, node_id: int, now: Optional[float] = None) -> dict[str, Any]:
        """Return the Pi-owned VOC decision state for API/dashboard consumers."""
        now = time.time() if now is None else now
        state = self._get_or_restore_voc_state(int(node_id))
        policy = self._cell_config.get_effective_voc_policy(node_id) or {}
        config = self._cell_config.get_cell_config(node_id) or {}
        timer_name: Optional[str] = None
        timer_since: Optional[float] = None
        required = 0
        requirement_score: Optional[int] = None
        latch = state["latch_severity"]
        if latch == "critical":
            decision_state = "critical"
        elif state.get("critical_since") is not None:
            decision_state, timer_name = "critical_pending", "critical"
            timer_since = state["critical_since"]
            required = policy.get("critical_seconds") or 0
            requirement_score = policy.get("critical_score")
        elif latch == "alert":
            decision_state = "alert"
        elif state.get("alert_since") is not None:
            decision_state, timer_name = "alert_pending", "alert"
            timer_since = state["alert_since"]
            required = policy.get("alert_seconds") or 0
            requirement_score = policy.get("alert_score")
        elif state.get("warning_clear_since") is not None:
            decision_state, timer_name = "warning_clear_pending", "warning_clear"
            timer_since = state["warning_clear_since"]
            required = policy.get("warning_clear_seconds") or 0
            requirement_score = policy.get("warning_clear_score")
        elif state["warning_active"]:
            decision_state = "warning"
        elif state.get("warning_since") is not None:
            decision_state, timer_name = "warning_pending", "warning"
            timer_since = state["warning_since"]
            required = policy.get("warning_seconds") or 0
            requirement_score = policy.get("warning_score")
        else:
            decision_state = "monitoring"
        elapsed = max(0.0, now - float(timer_since)) if timer_since is not None else 0.0
        progress = min(1.0, elapsed / required) if required > 0 else 0.0
        alert_latched = latch in {"alert", "critical"}
        return {
            "evaluation_state": decision_state,
            "active_timer": timer_name,
            "elapsed_seconds": elapsed,
            "required_seconds": required,
            "progress": progress,
            "requirement": {
                "score": requirement_score,
                "duration_seconds": required,
            } if timer_name else None,
            "effective_voc_policy": policy,
            "profile_id": config.get("profile_id"),
            "profile_name": config.get("profile_name"),
            "profile_source": config.get("profile_source"),
            "assignment_mode": config.get("assignment_mode"),
            "voc_policy_override": config.get("voc_policy_override"),
            "warning_active": bool(state["warning_active"]),
            "voc_latch_severity": latch,
            "voc_critical_latched": latch == "critical",
            "voc_decision_state": decision_state,
            "voc_warning_elapsed_seconds": (
                max(0.0, now - float(state["warning_since"]))
                if state.get("warning_since") is not None else 0.0
            ),
            "voc_alert_elapsed_seconds": (
                max(0.0, now - float(state["alert_since"]))
                if state.get("alert_since") is not None else 0.0
            ),
            "voc_alert_latched": alert_latched,
        }

    async def _fire_voc(
        self, node_id: int, severity: str, score: int, policy: dict[str, Any]
    ) -> None:
        cell_config = self._cell_config.get_cell_config(node_id)
        label = cell_config["label"] if cell_config else str(node_id)
        assignment = (
            (cell_config or {}).get("profile_name")
            or (cell_config or {}).get("category_name")
            or "Custom policy"
        )
        await self._notifier.send_voc_alert(
            cell_label=label,
            assignment=assignment,
            severity=severity,
            score=int(score),
            sigma=policy[f"{severity}_sigma"],
            sustain_minutes=policy[f"{severity}_seconds"] / 60,
        )

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
        previous_zone = state["previous_zone"]
        if previous_zone not in ("green", "yellow"):
            return
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
