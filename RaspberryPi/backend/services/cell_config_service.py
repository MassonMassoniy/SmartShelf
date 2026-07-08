from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Iterable, Optional

from ..config.constants import METRIC_LIMITS, PROJECT_NODE_IDS

METRICS = ("temperature", "humidity", "gas")
ZONES = ("green", "yellow", "red")

_DEFAULT_SIMPLE_BOUNDS = {
    "temperature": {"yellow_low": 18, "green_low": 22, "green_high": 27, "yellow_high": 30},
    "humidity": {"yellow_low": 30, "green_low": 40, "green_high": 60, "yellow_high": 70},
    "gas": {"yellow_low": 0, "green_low": 0, "green_high": 450, "yellow_high": 700},
}

_BACKEND_DIR = Path(__file__).resolve().parents[1]
_DEFAULT_CONFIG_PATH = _BACKEND_DIR / "config" / "config.json"
_DEFAULT_CATEGORIES_PATH = _BACKEND_DIR / "config" / "categories.json"


class CellConfigError(ValueError):
    """Raised for invalid cell configuration requests (unknown node/category, bad bounds)."""


def _simple_to_canonical(metric: str, simple: dict[str, Any]) -> dict[str, list[dict[str, float]]]:
    limits = METRIC_LIMITS[metric]
    floor = float(limits["min"])
    ceiling = float(limits["max"])

    try:
        yellow_low = float(simple["yellow_low"])
        green_low = float(simple["green_low"])
        green_high = float(simple["green_high"])
        yellow_high = float(simple["yellow_high"])
    except (KeyError, TypeError, ValueError) as exc:
        raise CellConfigError(
            f"'{metric}' bounds must include numeric yellow_low, green_low, green_high, yellow_high"
        ) from exc

    if not (floor <= yellow_low <= green_low <= green_high <= yellow_high <= ceiling):
        raise CellConfigError(
            f"'{metric}' bounds must satisfy {floor} <= yellow_low <= green_low <= green_high "
            f"<= yellow_high <= {ceiling} (got {yellow_low}, {green_low}, {green_high}, {yellow_high})"
        )

    yellow_bands = []
    if yellow_low < green_low:
        yellow_bands.append({"min": yellow_low, "max": green_low})
    if green_high < yellow_high:
        yellow_bands.append({"min": green_high, "max": yellow_high})

    red_bands = []
    if floor < yellow_low:
        red_bands.append({"min": floor, "max": yellow_low})
    if yellow_high < ceiling:
        red_bands.append({"min": yellow_high, "max": ceiling})

    return {
        "green": [{"min": green_low, "max": green_high}],
        "yellow": yellow_bands,
        "red": red_bands,
    }


def _canonical_to_simple(metric: str, canonical: dict[str, Any]) -> dict[str, float]:
    limits = METRIC_LIMITS[metric]
    green_bands = canonical.get("green") or []
    if green_bands:
        green_low = float(green_bands[0]["min"])
        green_high = float(green_bands[0]["max"])
    else:
        green_low = float(limits["min"])
        green_high = float(limits["max"])

    yellow_low = green_low
    yellow_high = green_high
    for band in canonical.get("yellow") or []:
        band_min = float(band["min"])
        band_max = float(band["max"])
        if band_max <= green_low:
            yellow_low = min(yellow_low, band_min)
        elif band_min >= green_high:
            yellow_high = max(yellow_high, band_max)

    return {
        "yellow_low": yellow_low,
        "green_low": green_low,
        "green_high": green_high,
        "yellow_high": yellow_high,
    }


def _default_canonical_bounds() -> dict[str, dict[str, list[dict[str, float]]]]:
    return {metric: _simple_to_canonical(metric, _DEFAULT_SIMPLE_BOUNDS[metric]) for metric in METRICS}


class CellConfigStore:
    """Loads/persists per-cell trigger bounds (category assignment or custom bounds).

    Backed by `config/config.json` (mutable, rewritten on every change) and the
    read-only `config/categories.json` predefined food-category catalog.
    """

    def __init__(
        self,
        config_path: str | Path = _DEFAULT_CONFIG_PATH,
        categories_path: str | Path = _DEFAULT_CATEGORIES_PATH,
    ) -> None:
        self._config_path = Path(config_path)
        self._categories_path = Path(categories_path)
        self._lock = threading.Lock()
        self._categories: dict[str, dict[str, Any]] = {}
        self._cells: dict[int, dict[str, Any]] = {}

        self._load_categories()
        self._load_cells()

    # -- loading -----------------------------------------------------------------

    def _load_categories(self) -> None:
        if not self._categories_path.exists():
            self._categories = {}
            return
        try:
            with open(self._categories_path, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except (OSError, ValueError):
            self._categories = {}
            return
        self._categories = dict(payload.get("categories", {})) if isinstance(payload, dict) else {}

    def _load_cells(self) -> None:
        raw: dict[str, Any] = {}
        if self._config_path.exists():
            try:
                with open(self._config_path, "r", encoding="utf-8") as f:
                    payload = json.load(f)
                if isinstance(payload, dict):
                    raw = dict(payload.get("trigger_values", {}))
            except (OSError, ValueError):
                raw = {}

        dirty = False
        cells: dict[int, dict[str, Any]] = {}
        for node_id, label in PROJECT_NODE_IDS.items():
            entry = raw.get(str(node_id))
            if isinstance(entry, dict) and all(metric in entry for metric in METRICS):
                cells[node_id] = {
                    "name": entry.get("name", label),
                    "mode": entry.get("mode", "custom"),
                    "category_id": entry.get("category_id"),
                    "temperature": entry["temperature"],
                    "humidity": entry["humidity"],
                    "gas": entry["gas"],
                }
            else:
                cells[node_id] = {
                    "name": label,
                    "mode": "custom",
                    "category_id": None,
                    **_default_canonical_bounds(),
                }
                dirty = True

        self._cells = cells
        if dirty:
            self._save()

    # -- persistence ---------------------------------------------------------------

    def _save(self) -> None:
        payload = {
            "trigger_values": {
                str(node_id): {
                    "name": entry["name"],
                    "mode": entry["mode"],
                    "category_id": entry["category_id"],
                    "temperature": entry["temperature"],
                    "humidity": entry["humidity"],
                    "gas": entry["gas"],
                }
                for node_id, entry in sorted(self._cells.items())
            }
        }
        os.makedirs(self._config_path.parent, exist_ok=True)
        temp_path = f"{self._config_path}.tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        os.replace(temp_path, self._config_path)

    # -- reads -----------------------------------------------------------------

    def list_categories(self) -> dict[str, dict[str, Any]]:
        return self._categories

    def get_metric_limits(self) -> dict[str, dict[str, float]]:
        return METRIC_LIMITS

    def _effective_bounds_locked(self, node_id: int) -> dict[str, Any]:
        entry = self._cells[node_id]
        if entry["mode"] == "category":
            category = self._categories.get(entry["category_id"] or "")
            if category is not None:
                return {metric: category[metric] for metric in METRICS}
        return {metric: entry[metric] for metric in METRICS}

    def get_effective_bounds(self, node_id: int) -> Optional[dict[str, Any]]:
        with self._lock:
            if node_id not in self._cells:
                return None
            return self._effective_bounds_locked(node_id)

    def _cell_config_locked(self, node_id: int) -> dict[str, Any]:
        entry = self._cells[node_id]
        bounds = self._effective_bounds_locked(node_id)
        simple_bounds = {metric: _canonical_to_simple(metric, bounds[metric]) for metric in METRICS}
        category = self._categories.get(entry["category_id"] or "")
        return {
            "node_id": node_id,
            "label": PROJECT_NODE_IDS.get(node_id, entry["name"]),
            "mode": entry["mode"],
            "category_id": entry["category_id"],
            "category_name": category["name"] if category else None,
            "bounds": bounds,
            "simple_bounds": simple_bounds,
        }

    def get_cell_config(self, node_id: int) -> Optional[dict[str, Any]]:
        with self._lock:
            if node_id not in self._cells:
                return None
            return self._cell_config_locked(node_id)

    def get_all_cell_configs(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self._cell_config_locked(node_id) for node_id in sorted(self._cells.keys())]

    # -- writes -----------------------------------------------------------------

    def set_category(self, node_id: int, category_id: str) -> dict[str, Any]:
        with self._lock:
            if node_id not in self._cells:
                raise CellConfigError(f"Unknown cell node_id {node_id}")
            category = self._categories.get(category_id)
            if category is None:
                raise CellConfigError(f"Unknown category_id {category_id!r}")

            entry = self._cells[node_id]
            entry["mode"] = "category"
            entry["category_id"] = category_id
            for metric in METRICS:
                entry[metric] = category[metric]
            self._save()
            return self._cell_config_locked(node_id)

    def set_custom_bounds(self, node_id: int, simple_bounds: dict[str, dict[str, Any]]) -> dict[str, Any]:
        with self._lock:
            if node_id not in self._cells:
                raise CellConfigError(f"Unknown cell node_id {node_id}")
            missing = [metric for metric in METRICS if metric not in simple_bounds]
            if missing:
                raise CellConfigError(f"Missing bounds for metric(s): {', '.join(missing)}")

            canonical = {metric: _simple_to_canonical(metric, simple_bounds[metric]) for metric in METRICS}

            entry = self._cells[node_id]
            entry["mode"] = "custom"
            entry["category_id"] = None
            for metric in METRICS:
                entry[metric] = canonical[metric]
            self._save()
            return self._cell_config_locked(node_id)

    def project_node_ids(self) -> Iterable[int]:
        return PROJECT_NODE_IDS.keys()
