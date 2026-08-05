from __future__ import annotations

import json
import threading
import time
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterable, Optional

from ..config.constants import METRIC_LIMITS, PROJECT_NODE_IDS
from ..storage.sqlite.sqlite_storage import SQLiteStorage

METRICS = ("temperature", "humidity", "gas")
# Shared product profiles no longer carry VOC Index zone bounds; cells keep an
# inert default gas band only for legacy storage / non-v5 paths.
PROFILE_METRICS = ("temperature", "humidity")
ZONES = ("green", "yellow", "red")
VOC_SCORE_DURATION_SECONDS = 30 * 60
DEFAULT_VOC_POLICY = {
    "warning_score": 25,
    "warning_sigma": 6,
    "warning_seconds": VOC_SCORE_DURATION_SECONDS,
    "warning_clear_score": 20,
    "warning_clear_seconds": 5 * 60,
    "alert_score": 50,
    "alert_sigma": 10,
    "alert_seconds": VOC_SCORE_DURATION_SECONDS,
    "critical_score": 75,
    "critical_sigma": 10,
    "critical_seconds": VOC_SCORE_DURATION_SECONDS,
}

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


def _validate_voc_policy(policy: Any) -> dict[str, Any]:
    if policy is None:
        return dict(DEFAULT_VOC_POLICY)
    if not isinstance(policy, dict):
        raise CellConfigError("'voc_policy' must be an object")
    result: dict[str, Any] = {}
    for key, default in DEFAULT_VOC_POLICY.items():
        raw = policy.get(key, default)
        # Legacy rows may still store null critical fields from when critical
        # was optional; treat missing/null as the current default.
        if raw is None:
            raw = default
        try:
            value = int(raw)
        except (TypeError, ValueError) as exc:
            raise CellConfigError(f"'voc_policy.{key}' must be an integer") from exc
        if key.endswith("_seconds") and value <= 0:
            raise CellConfigError(f"'voc_policy.{key}' must be positive")
        if not key.endswith("_seconds") and value < 0:
            raise CellConfigError(f"'voc_policy.{key}' must be non-negative")
        result[key] = value
    # Product UI only edits scores; persistence/evaluation always use fixed windows.
    result["warning_seconds"] = VOC_SCORE_DURATION_SECONDS
    result["alert_seconds"] = VOC_SCORE_DURATION_SECONDS
    result["critical_seconds"] = VOC_SCORE_DURATION_SECONDS
    result["warning_sigma"] = 6
    result["alert_sigma"] = 10
    result["critical_sigma"] = 10
    if policy.get("warning_sigma") not in (None, 6) or policy.get("alert_sigma") not in (
        None,
        10,
    ) or policy.get("critical_sigma") not in (None, 10):
        raise CellConfigError(
            "VOC evidence gates are fixed at warning=6 sigma, alert=10 sigma, critical=10 sigma"
        )
    if result["warning_clear_score"] >= result["warning_score"]:
        result["warning_clear_score"] = max(0, result["warning_score"] - 5)
    score_keys = ("warning_clear_score", "warning_score", "alert_score", "critical_score")
    if any(result[key] > 100 for key in score_keys):
        raise CellConfigError("VOC scores must be between 0 and 100")
    if not (result["warning_score"] <= result["alert_score"] <= result["critical_score"]):
        raise CellConfigError("VOC scores must satisfy warning ≤ alert ≤ critical")
    return result


def _validate_voc_override(override: Any, base: dict[str, Any]) -> dict[str, Any]:
    if override is None:
        return {}
    if not isinstance(override, dict):
        raise CellConfigError("'voc_policy_override' must be an object")
    unknown = set(override) - set(DEFAULT_VOC_POLICY)
    if unknown:
        raise CellConfigError(f"Unknown VOC override field(s): {', '.join(sorted(unknown))}")
    merged = dict(base)
    merged.update(override)
    validated = _validate_voc_policy(merged)
    return {key: validated[key] for key in override}


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
        self._profiles: dict[str, dict[str, Any]] = {}
        self._cells: dict[int, dict[str, Any]] = {}
        self._loaded = False

    def load(self) -> None:
        """Load categories + cell configs from SQLite (seed from JSON if empty)."""
        with self._lock:
            self._load_categories()
            self._load_profiles()
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

    def _load_profiles(self) -> None:
        self._profiles = self._storage.load_product_profiles()
        for profile in self._profiles.values():
            normalized = _validate_voc_policy(profile.get("voc_policy"))
            if profile.get("voc_policy") != normalized:
                profile["voc_policy"] = normalized
                self._storage.upsert_product_profile(profile)
        for category_id, category in self._categories.items():
            existing = next(
                (
                    profile
                    for profile in self._profiles.values()
                    if profile.get("source_category_id") == category_id
                ),
                None,
            )
            if existing is not None:
                continue
            profile_id = category_id if category_id not in self._profiles else f"category-{category_id}"
            defaults = _default_canonical_bounds()
            profile = {
                "profile_id": profile_id,
                "name": category.get("name", category_id),
                "source_category_id": category_id,
                "voc_policy": _validate_voc_policy(category.get("voc_policy")),
                **{
                    metric: deepcopy(category[metric])
                    if isinstance(category.get(metric), dict)
                    else defaults[metric]
                    for metric in PROFILE_METRICS
                },
                "gas": defaults["gas"],
            }
            self._storage.upsert_product_profile(profile)
            self._profiles[profile_id] = profile

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
        category = self._categories.get(str(entry.get("category_id") or ""))
        return {
            "name": entry.get("name", label),
            "mode": entry.get("mode", "custom"),
            "category_id": entry.get("category_id"),
            "temperature": entry["temperature"],
            "humidity": entry["humidity"],
            "gas": entry["gas"],
            "voc_policy": _validate_voc_policy(
                entry.get("voc_policy")
                if entry.get("voc_policy") is not None
                else (category or {}).get("voc_policy")
            ),
            "assignment_mode": (
                entry.get("assignment_mode")
                or ("linked" if entry.get("mode") == "category" else "custom")
            ),
            "profile_id": entry.get("profile_id") or entry.get("category_id"),
            "profile_snapshot": entry.get("profile_snapshot"),
            "voc_policy_override": entry.get("voc_policy_override"),
        }

    def _default_entry(self, label: str) -> dict[str, Any]:
        return {
            "name": label,
            "mode": "custom",
            "category_id": None,
            "voc_policy": dict(DEFAULT_VOC_POLICY),
            "assignment_mode": "custom",
            "profile_id": None,
            "profile_snapshot": None,
            "voc_policy_override": None,
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
                mode = cells[node_id].get("assignment_mode")
                if mode not in {"linked", "copied", "override", "custom"}:
                    mode = "linked" if cells[node_id].get("mode") == "category" else "custom"
                    cells[node_id]["assignment_mode"] = mode
                    dirty = True
                if mode in {"linked", "override"} and not cells[node_id].get("profile_id"):
                    category_id = cells[node_id].get("category_id")
                    if category_id in self._profiles:
                        cells[node_id]["profile_id"] = category_id
                        dirty = True
                if mode in {"linked", "override"}:
                    profile = self._profiles.get(cells[node_id].get("profile_id") or "")
                    if profile is not None:
                        self._apply_effective_profile(cells[node_id], profile)
                        dirty = True
                else:
                    cells[node_id]["voc_policy"] = _validate_voc_policy(
                        cells[node_id].get("voc_policy")
                    )
                    dirty = True
                if cells[node_id].get("voc_policy") is None:
                    category = self._categories.get(str(cells[node_id].get("category_id") or ""))
                    cells[node_id]["voc_policy"] = _validate_voc_policy(
                        (category or {}).get("voc_policy")
                    )
                    dirty = True
                continue

            seeded = self._entry_from_raw(node_id, label, seed.get(str(node_id)))
            if seeded is not None:
                cells[node_id] = seeded
            else:
                cells[node_id] = self._default_entry(label)
            dirty = True

        # Keep deployments' non-project/legacy rows intact. They are not
        # evaluated unless separately configured as project nodes, but an
        # additive migration must never discard them.
        for node_id, entry in stored.items():
            if node_id in cells:
                continue
            entry["assignment_mode"] = (
                entry.get("assignment_mode")
                or ("linked" if entry.get("mode") == "category" else "custom")
            )
            entry["profile_id"] = entry.get("profile_id") or entry.get("category_id")
            entry["profile_snapshot"] = entry.get("profile_snapshot")
            entry["voc_policy_override"] = entry.get("voc_policy_override")
            entry["voc_policy"] = _validate_voc_policy(entry.get("voc_policy"))
            if entry["assignment_mode"] in {"linked", "override"}:
                profile = self._profiles.get(entry.get("profile_id") or "")
                if profile is not None:
                    self._apply_effective_profile(entry, profile)
            cells[node_id] = entry
            dirty = True

        self._cells = cells
        if dirty or not stored:
            # Persist migrated/default rows into the existing DB.
            self._storage.replace_cell_configs(self._cells)

    def _apply_effective_profile(
        self, entry: dict[str, Any], profile: dict[str, Any]
    ) -> None:
        for metric in PROFILE_METRICS:
            entry[metric] = deepcopy(profile[metric])
        # Profiles do not own VOC Index bands; keep any existing cell value or
        # fall back to the inert default so storage remains complete.
        if not isinstance(entry.get("gas"), dict):
            entry["gas"] = _default_canonical_bounds()["gas"]
        override = entry.get("voc_policy_override") or {}
        base = _validate_voc_policy(profile.get("voc_policy"))
        override = _validate_voc_override(override, base)
        effective = dict(base)
        effective.update(override)
        entry["voc_policy_override"] = override or None
        entry["voc_policy"] = _validate_voc_policy(effective)

    # -- persistence ---------------------------------------------------------------

    def _save_node(self, node_id: int) -> None:
        self._storage.upsert_cell_config(node_id, self._cells[node_id])

    # -- reads -----------------------------------------------------------------

    def list_categories(self) -> dict[str, dict[str, Any]]:
        self._ensure_loaded()
        return self._categories

    def list_profiles(self) -> list[dict[str, Any]]:
        with self._lock:
            self._ensure_loaded()
            return [self._profile_response(profile) for profile in self._profiles.values()]

    @staticmethod
    def _profile_response(profile: dict[str, Any]) -> dict[str, Any]:
        return {
            key: deepcopy(profile.get(key))
            for key in (
                "profile_id", "name", "temperature", "humidity", "voc_policy",
                "source_category_id", "created_at", "updated_at"
            )
            if key in profile
        }

    def get_profile(self, profile_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            self._ensure_loaded()
            profile = self._profiles.get(profile_id)
            return self._profile_response(profile) if profile else None

    def get_metric_limits(self) -> dict[str, dict[str, float]]:
        return METRIC_LIMITS

    def _effective_bounds_locked(self, node_id: int) -> dict[str, Any]:
        entry = self._cells[node_id]
        return {metric: entry[metric] for metric in METRICS}

    def get_effective_bounds(self, node_id: int) -> Optional[dict[str, Any]]:
        with self._lock:
            self._ensure_loaded()
            if node_id not in self._cells:
                return None
            return self._effective_bounds_locked(node_id)

    def get_effective_voc_policy(self, node_id: int) -> Optional[dict[str, Any]]:
        with self._lock:
            self._ensure_loaded()
            if node_id not in self._cells:
                return None
            return _validate_voc_policy(self._cells[node_id].get("voc_policy"))

    def _cell_config_locked(self, node_id: int) -> dict[str, Any]:
        entry = self._cells[node_id]
        bounds = self._effective_bounds_locked(node_id)
        simple_bounds = {metric: _canonical_to_simple(metric, bounds[metric]) for metric in METRICS}
        category = self._categories.get(entry["category_id"] or "")
        profile = self._profiles.get(entry.get("profile_id") or "")
        assignment_mode = entry.get("assignment_mode") or "custom"
        return {
            "node_id": node_id,
            "label": PROJECT_NODE_IDS.get(node_id, entry["name"]),
            "mode": entry["mode"],
            "assignment_mode": assignment_mode,
            "category_id": entry["category_id"],
            "category_name": category["name"] if category else None,
            "profile_id": entry.get("profile_id"),
            "profile_name": profile.get("name") if profile else (
                (entry.get("profile_snapshot") or {}).get("name")
            ),
            "profile_source": (
                "live" if assignment_mode in {"linked", "override"}
                else "snapshot" if assignment_mode == "copied"
                else "custom"
            ),
            "profile_snapshot": deepcopy(entry.get("profile_snapshot")),
            "voc_policy_override": deepcopy(entry.get("voc_policy_override")),
            "bounds": bounds,
            "simple_bounds": simple_bounds,
            "voc_policy": _validate_voc_policy(entry.get("voc_policy")),
            "effective_voc_policy": _validate_voc_policy(entry.get("voc_policy")),
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

    def _validated_profile_payload(
        self, payload: Any, profile_id: Optional[str] = None
    ) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise CellConfigError("Profile payload must be an object")
        name = str(payload.get("name") or "").strip()
        if not name:
            raise CellConfigError("Profile name is required")
        bounds: dict[str, Any] = {}
        for metric in PROFILE_METRICS:
            value = payload.get(metric)
            if not isinstance(value, dict):
                raise CellConfigError(f"Profile '{metric}' bounds are required")
            # Round-trip validation accepts canonical profile bounds.
            bounds[metric] = _simple_to_canonical(metric, _canonical_to_simple(metric, value))
        # Persist an inert gas band for schema compatibility; API omits it.
        bounds["gas"] = _default_canonical_bounds()["gas"]
        return {
            "profile_id": profile_id or str(payload.get("profile_id") or uuid.uuid4()),
            "name": name,
            "voc_policy": _validate_voc_policy(payload.get("voc_policy")),
            **bounds,
        }

    def create_profile(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._ensure_loaded()
            profile = self._validated_profile_payload(payload)
            if profile["profile_id"] in self._profiles:
                raise CellConfigError(f"Profile {profile['profile_id']!r} already exists")
            now = time.time()
            profile.update(created_at=now, updated_at=now, source_category_id=None)
            self._storage.upsert_product_profile(profile)
            self._profiles[profile["profile_id"]] = profile
            return self._profile_response(profile)

    def update_profile(self, profile_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._ensure_loaded()
            old = self._profiles.get(profile_id)
            if old is None:
                raise CellConfigError(f"Unknown profile_id {profile_id!r}")
            merged = deepcopy(old)
            merged.update(payload)
            profile = self._validated_profile_payload(merged, profile_id=profile_id)
            profile.update(
                created_at=old.get("created_at", time.time()),
                updated_at=time.time(),
                source_category_id=old.get("source_category_id"),
            )
            changed_cells: dict[int, dict[str, Any]] = {}
            for node_id, entry in self._cells.items():
                if (
                    entry.get("profile_id") == profile_id
                    and entry.get("assignment_mode") in {"linked", "override"}
                ):
                    self._apply_effective_profile(entry, profile)
                    changed_cells[node_id] = entry
            self._storage.save_profile_and_cells(profile, changed_cells)
            self._profiles[profile_id] = profile
            return self._profile_response(profile)

    def delete_profile(self, profile_id: str) -> None:
        with self._lock:
            self._ensure_loaded()
            if profile_id not in self._profiles:
                raise CellConfigError(f"Unknown profile_id {profile_id!r}")
            linked = [
                node_id for node_id, entry in self._cells.items()
                if entry.get("profile_id") == profile_id
                and entry.get("assignment_mode") in {"linked", "override"}
            ]
            if linked:
                raise CellConfigError(
                    f"Profile is assigned to cell(s): {', '.join(map(str, linked))}"
                )
            self._storage.delete_product_profile(profile_id)
            del self._profiles[profile_id]

    def set_assignment(
        self,
        node_id: int,
        assignment_mode: str,
        profile_id: Optional[str] = None,
        voc_policy_override: Optional[dict[str, Any]] = None,
        custom_bounds: Optional[dict[str, dict[str, Any]]] = None,
        voc_policy: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        with self._lock:
            self._ensure_loaded()
            if node_id not in self._cells:
                raise CellConfigError(f"Unknown cell node_id {node_id}")
            if assignment_mode not in {"linked", "copied", "override", "custom"}:
                raise CellConfigError(
                    "assignment_mode must be linked, copied, override, or custom"
                )
            entry = self._cells[node_id]
            if assignment_mode == "custom":
                if custom_bounds is not None:
                    missing = [
                        metric for metric in PROFILE_METRICS if metric not in custom_bounds
                    ]
                    if missing:
                        raise CellConfigError(
                            f"Missing bounds for metric(s): {', '.join(missing)}"
                        )
                    for metric in PROFILE_METRICS:
                        entry[metric] = _simple_to_canonical(metric, custom_bounds[metric])
                    if "gas" in custom_bounds:
                        entry["gas"] = _simple_to_canonical("gas", custom_bounds["gas"])
                    elif not isinstance(entry.get("gas"), dict):
                        entry["gas"] = _default_canonical_bounds()["gas"]
                entry["voc_policy"] = _validate_voc_policy(
                    voc_policy if voc_policy is not None else entry.get("voc_policy")
                )
                entry.update(
                    mode="custom", category_id=None, assignment_mode="custom",
                    profile_id=None, profile_snapshot=None, voc_policy_override=None,
                )
            else:
                profile = self._profiles.get(str(profile_id or ""))
                if profile is None:
                    raise CellConfigError(f"Unknown profile_id {profile_id!r}")
                entry.update(
                    mode="category",
                    category_id=profile.get("source_category_id"),
                    assignment_mode=assignment_mode,
                    profile_id=profile["profile_id"],
                    profile_snapshot=None,
                    voc_policy_override=None,
                )
                if assignment_mode == "copied":
                    defaults = _default_canonical_bounds()
                    snapshot = {
                        "profile_id": profile["profile_id"],
                        "name": profile["name"],
                        "voc_policy": deepcopy(profile["voc_policy"]),
                        **{
                            metric: deepcopy(profile[metric])
                            for metric in PROFILE_METRICS
                        },
                        "gas": deepcopy(profile.get("gas") or defaults["gas"]),
                    }
                    entry["profile_snapshot"] = snapshot
                    for metric in METRICS:
                        entry[metric] = deepcopy(snapshot[metric])
                    entry["voc_policy"] = deepcopy(snapshot["voc_policy"])
                else:
                    if assignment_mode == "override":
                        entry["voc_policy_override"] = _validate_voc_override(
                            voc_policy_override, profile["voc_policy"]
                        )
                    self._apply_effective_profile(entry, profile)
            self._save_node(node_id)
            return self._cell_config_locked(node_id)

    def set_category(
        self, node_id: int, category_id: str, voc_policy: Optional[dict[str, Any]] = None
    ) -> dict[str, Any]:
        with self._lock:
            self._ensure_loaded()
            if node_id not in self._cells:
                raise CellConfigError(f"Unknown cell node_id {node_id}")
            category = self._categories.get(category_id)
            if category is None:
                raise CellConfigError(f"Unknown category_id {category_id!r}")

        profile = next(
            (
                profile for profile in self._profiles.values()
                if profile.get("source_category_id") == category_id
            ),
            None,
        )
        if profile is None:
            raise CellConfigError(f"No profile migrated for category_id {category_id!r}")
        mode = "override" if voc_policy is not None else "linked"
        return self.set_assignment(
            node_id, mode, profile_id=profile["profile_id"],
            voc_policy_override=voc_policy,
        )

    def set_custom_bounds(
        self,
        node_id: int,
        simple_bounds: dict[str, dict[str, Any]],
        voc_policy: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        return self.set_assignment(
            node_id, "custom", custom_bounds=simple_bounds, voc_policy=voc_policy
        )

    def set_voc_policy(self, node_id: int, voc_policy: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._ensure_loaded()
            if node_id not in self._cells:
                raise CellConfigError(f"Unknown cell node_id {node_id}")
            entry = self._cells[node_id]
            if entry.get("assignment_mode") in {"linked", "override"}:
                profile = self._profiles.get(entry.get("profile_id") or "")
                if profile is None:
                    raise CellConfigError("Assigned profile no longer exists")
                entry["assignment_mode"] = "override"
                entry["voc_policy_override"] = _validate_voc_override(
                    voc_policy, profile["voc_policy"]
                )
                self._apply_effective_profile(entry, profile)
            else:
                entry["voc_policy"] = _validate_voc_policy(voc_policy)
            self._save_node(node_id)
            return self._cell_config_locked(node_id)

    def project_node_ids(self) -> Iterable[int]:
        return PROJECT_NODE_IDS.keys()
