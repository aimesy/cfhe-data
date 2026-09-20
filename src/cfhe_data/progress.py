# visibility: public
"""Linear RHNA progress at the same reporting cutoff as credited units."""

from __future__ import annotations

from datetime import date
from fractions import Fraction
from math import ceil


def calculate_progress(
    credited_units: int,
    allocation: int,
    planning_start: date,
    planning_end: date,
    data_through: date,
) -> dict[str, str | float | int | bool | None]:
    """Compare credited units with a linear benchmark over the actual period.

    Dates measure elapsed days from the planning start to the reporting cutoff,
    clamped to the planning period. Exact rational arithmetic determines the
    whole-unit minimum; rounded display values never decide whether a locality
    is on pace. A zero allocation or an unstarted period has no pace judgment.
    """
    for name, value in (("credited_units", credited_units), ("allocation", allocation)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{name} must be a nonnegative integer")
    period_days = (planning_end - planning_start).days
    if period_days <= 0:
        raise ValueError("planning_end must be after planning_start")

    elapsed_days = min(max((data_through - planning_start).days, 0), period_days)
    elapsed_fraction = Fraction(elapsed_days, period_days)
    expected_units = allocation * elapsed_fraction
    minimum_units = ceil(expected_units)

    on_pace = None
    if allocation == 0:
        status = "no_allocation"
    elif elapsed_days == 0:
        status = "not_started"
    else:
        on_pace = credited_units >= minimum_units
        status = "on_pace" if on_pace else "behind_pace"

    return {
        "data_through": data_through.isoformat(),
        "elapsed_fraction": float(elapsed_fraction),
        "expected_units": float(expected_units),
        "minimum_units_for_pace": minimum_units,
        "progress_fraction": float(Fraction(credited_units, allocation))
        if allocation
        else None,
        "on_pace": on_pace,
        "status": status,
    }
