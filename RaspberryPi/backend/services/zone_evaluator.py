from __future__ import annotations

from typing import Any, Literal, Optional

Zone = Literal["green", "yellow", "red"]
ZONE_ORDER: tuple[Zone, ...] = ("green", "yellow", "red")

_BAR_CLASS_BY_ZONE = {
    "green": "bg-emerald-500",
    "yellow": "bg-amber-400",
    "red": "bg-red-500",
}
_BADGE_CLASS_BY_ZONE = {
    "green": "bg-emerald-100 text-emerald-700",
    "yellow": "bg-amber-100 text-amber-800",
    "red": "bg-red-100 text-red-700",
}
_UNKNOWN_BAR_CLASS = "bg-slate-300"
_UNKNOWN_BADGE_CLASS = "bg-slate-200 text-slate-500"


def _value_in_ranges(value: float, ranges: Any) -> bool:
    if not isinstance(ranges, list):
        return False
    for band in ranges:
        try:
            band_min = float(band["min"])
            band_max = float(band["max"])
        except (KeyError, TypeError, ValueError):
            continue
        if band_min <= value <= band_max:
            return True
    return False


def classify_zone(value: Optional[float], bounds: Optional[dict[str, Any]]) -> Optional[Zone]:
    """Classify a metric reading into green/yellow/red using the given bounds.

    `bounds` is the canonical per-metric shape: {"green": [...], "yellow": [...], "red": [...]}
    where each entry is a list of {"min": x, "max": y} bands. Zones are checked in
    green -> yellow -> red order, so shared boundary values resolve to the safer zone.
    Returns None when the value or bounds are missing, or the value falls outside
    every defined band.
    """
    if value is None or bounds is None:
        return None
    try:
        numeric_value = float(value)
    except (TypeError, ValueError):
        return None

    for zone in ZONE_ORDER:
        if _value_in_ranges(numeric_value, bounds.get(zone)):
            return zone
    return None


def zone_severity(zone: Optional[Zone]) -> int:
    """Higher is worse. Unknown zones are treated as neutral (0), same as green."""
    if zone is None:
        return 0
    return ZONE_ORDER.index(zone)


def is_degradation(previous_zone: Optional[Zone], new_zone: Optional[Zone]) -> bool:
    """True when new_zone is strictly worse than previous_zone."""
    return zone_severity(new_zone) > zone_severity(previous_zone)


def zone_bar_class(zone: Optional[Zone]) -> str:
    return _BAR_CLASS_BY_ZONE.get(zone, _UNKNOWN_BAR_CLASS)


def zone_badge_class(zone: Optional[Zone]) -> str:
    return _BADGE_CLASS_BY_ZONE.get(zone, _UNKNOWN_BADGE_CLASS)
