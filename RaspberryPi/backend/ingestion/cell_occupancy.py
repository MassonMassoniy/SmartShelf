from __future__ import annotations

from typing import Any, Literal, Optional

from ..config.constants import EMPTY_CELL_PROXIMITY_THRESHOLD

CellStatus = Literal["empty", "full", "unknown"]


def cell_status(proximity: Optional[float]) -> CellStatus:
    """Classify shelf occupancy from proximity, or unknown when not reported."""
    if proximity is None:
        return "unknown"
    if float(proximity) <= EMPTY_CELL_PROXIMITY_THRESHOLD:
        return "empty"
    return "full"


def is_cell_empty(proximity: Optional[float]) -> bool:
    """Return True when proximity indicates the shelf cell has no product."""
    return cell_status(proximity) == "empty"


def with_cell_occupancy(sample: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(sample)
    status = cell_status(enriched.get("proximity"))
    enriched["cell_status"] = status
    enriched["cell_empty"] = status == "empty"
    return enriched
