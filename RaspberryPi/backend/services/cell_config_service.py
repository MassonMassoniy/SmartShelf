from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Iterable, Optional

from ..config.constants import METRIC_LIMITS, PROJECT_NODE_IDS
from ..storage.sqlite.sqlite_storage import SQLiteStorage

METRICS = ("temperature", "humidity", "gas")
ZONES = ("green", "yellow", "red")

_DEFAULT_SIMPLE_BOUNDS = {
    "temperature": {"yellow_low": 18, "green_low": 22, "green_high": 27, "yellow_high": 30},
    "humidity": {"yellow_low": 30, "green_low": 40, "green_high": 60, "yellow_high": 70},
    "gas": {"yellow_low": 0, "green_low": 0, "green_high": 450, "yellow_high": 700},
}

_BACKEND_DIR = Path(__file__).resolve().parents[1]
_DEFAULT_SEED_CONFIG_PATH = _BACKEND_DIR / "config" / "config.json"
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

    Backed by the `cell_config` table in SQLite. On first startup with an empty
    table, seeds from `config/config.json` when present (one-time migration).
    Categories remain in read-only `config/categories.json`.
    """

    def __init__(
        self,
        storage: SQLiteStorage,
        seed_config_path: str | Path = _DEFAULT_SEED_CONFIG_PATH,
        categories_path: str | Path = _DEFAULT_CATEGORIES_PATH,
    ) -> None:
        self._storage = storage
        self._seed_config_path = Path(seed_config_path)
        self._categories_path = Path(categories_path)
        self._lock = threading.Lock()
        self._categories: dict[str, dict[str, Any]] = {}
        self._cells: dict[int, dict[str, Any]] = {}
        self._loaded = False

    def load(self) -> None:
        """Load categories + cell configs from SQLite (seed from JSON if empty)."""
        with self._lock:
            self._load_categories()
            self._load_cells()
            self._loaded = True

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            raise RuntimeError("CellConfigStore.load() must be called after SQLiteStorage.start()")

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

    def _read_seed_trigger_values(self) -> dict[str, Any]:
        if not self._seed_config_path.exists():
            return {}
        try:
            with open(self._seed_config_path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            if isinstance(payload, dict):
                return dict(payload.get("trigger_values", {}))
        except (OSError, ValueError):
            return {}
        return {}

    def _entry_from_raw(self, node_id: int, label: str, entry: Any) -> dict[str, Any] | None:
        if not isinstance(entry, dict) or not all(metric in entry for metric in METRICS):
            return None
        return {
            "name": entry.get("name", label),
            "mode": entry.get("mode", "custom"),
            "category_id": entry.get("category_id"),
            "temperature": entry["temperature"],
            "humidity": entry["humidity"],
            "gas": entry["gas"],
        }

    def _default_entry(self, label: str) -> dict[str, Any]:
        return {
            "name": label,
            "mode": "custom",
            "category_id": None,
            **_default_canonical_bounds(),
        }

    def _load_cells(self) -> None:
        stored = self._storage.load_cell_configs()
        seed = self._read_seed_trigger_values() if not stored else {}

        dirty = False
        cells: dict[int, dict[str, Any]] = {}
        for node_id, label in PROJECT_NODE_IDS.items():
            if node_id in stored:
                cells[node_id] = stored[node_id]
                continue

            seeded = self._entry_from_raw(node_id, label, seed.get(str(node_id)))
            if seeded is not None:
                cells[node_id] = seeded
            else:
                cells[node_id] = self._default_entry(label)
            dirty = True

        self._cells = cells
        if dirty or not stored:
            # Persist migrated/default rows into the existing DB.
            self._storage.replace_cell_configs(self._cells)

    # -- persistence ---------------------------------------------------------------

    def _save_node(self, node_id: int) -> None:
        self._storage.upsert_cell_config(node_id, self._cells[node_id])

    # -- reads -----------------------------------------------------------------

    def list_categories(self) -> dict[str, dict[str, Any]]:
        self._ensure_loaded()
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
            self._ensure_loaded()
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
            self._ensure_loaded()
            if node_id not in self._cells:
                return None
            return self._cell_config_locked(node_id)

    def get_all_cell_configs(self) -> list[dict[str, Any]]:
        with self._lock:
            self._ensure_loaded()
            return [self._cell_config_locked(node_id) for node_id in sorted(self._cells.keys())]

    # -- writes -----------------------------------------------------------------

    def set_category(self, node_id: int, category_id: str) -> dict[str, Any]:
        with self._lock:
            self._ensure_loaded()
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
            self._save_node(node_id)
            return self._cell_config_locked(node_id)

    def set_custom_bounds(self, node_id: int, simple_bounds: dict[str, dict[str, Any]]) -> dict[str, Any]:
        with self._lock:
            self._ensure_loaded()
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
            self._save_node(node_id)
            return self._cell_config_locked(node_id)

    def project_node_ids(self) -> Iterable[int]:
        return PROJECT_NODE_IDS.keys()
