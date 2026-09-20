# visibility: public
from datetime import date, timedelta

import pytest

from cfhe_data.progress import calculate_progress

START = date(2021, 10, 15)
END = date(2029, 10, 15)


def test_600_unit_allocation_requires_300_at_halfway() -> None:
    result = calculate_progress(300, 600, START, END, date(2025, 10, 15))

    assert result == {
        "data_through": "2025-10-15",
        "elapsed_fraction": 0.5,
        "expected_units": 300.0,
        "minimum_units_for_pace": 300,
        "progress_fraction": 0.5,
        "on_pace": True,
        "status": "on_pace",
    }
    behind = calculate_progress(299, 600, START, END, date(2025, 10, 15))
    assert behind["on_pace"] is False
    assert behind["status"] == "behind_pace"


@pytest.mark.parametrize("cutoff", [START - timedelta(days=1), START])
def test_before_and_at_planning_start_have_no_pace_judgment(cutoff: date) -> None:
    result = calculate_progress(10, 600, START, END, cutoff)

    assert result["elapsed_fraction"] == 0
    assert result["expected_units"] == 0
    assert result["minimum_units_for_pace"] == 0
    assert result["progress_fraction"] == 10 / 600
    assert result["on_pace"] is None
    assert result["status"] == "not_started"


@pytest.mark.parametrize("cutoff", [END, END + timedelta(days=365)])
def test_end_and_later_require_full_allocation(cutoff: date) -> None:
    result = calculate_progress(600, 600, START, END, cutoff)

    assert result["data_through"] == cutoff.isoformat()
    assert result["elapsed_fraction"] == 1
    assert result["expected_units"] == 600
    assert result["minimum_units_for_pace"] == 600
    assert result["on_pace"] is True
    assert calculate_progress(599, 600, START, END, cutoff)["on_pace"] is False


def test_reporting_cutoff_and_actual_five_year_period_control_pace() -> None:
    result = calculate_progress(
        366, 1826, date(2020, 1, 1), date(2025, 1, 1), date(2021, 1, 1)
    )

    assert result["elapsed_fraction"] == 366 / 1827
    assert result["minimum_units_for_pace"] == 366
    assert result["on_pace"] is True
    assert result["data_through"] == "2021-01-01"


def test_fractional_benchmark_requires_next_whole_unit() -> None:
    result = calculate_progress(
        3, 10, date(2025, 1, 1), date(2025, 1, 4), date(2025, 1, 2)
    )

    assert result["expected_units"] == 10 / 3
    assert result["minimum_units_for_pace"] == 4
    assert result["on_pace"] is False


def test_exact_integer_threshold_is_not_raised_by_float_rounding() -> None:
    result = calculate_progress(
        7, 100, date(2025, 1, 1), date(2025, 4, 11), date(2025, 1, 8)
    )

    assert result["expected_units"] == 7
    assert result["minimum_units_for_pace"] == 7
    assert result["on_pace"] is True


def test_credit_above_allocation_is_preserved() -> None:
    result = calculate_progress(700, 600, START, END, END)

    assert result["progress_fraction"] == 700 / 600
    assert result["on_pace"] is True


@pytest.mark.parametrize("credited", [0, 10])
def test_zero_allocation_has_no_progress_fraction_or_pace_judgment(
    credited: int,
) -> None:
    result = calculate_progress(credited, 0, START, END, END)

    assert result["expected_units"] == 0
    assert result["minimum_units_for_pace"] == 0
    assert result["progress_fraction"] is None
    assert result["on_pace"] is None
    assert result["status"] == "no_allocation"


@pytest.mark.parametrize("invalid", [-1, True, False, 1.5, "1", None])
@pytest.mark.parametrize("field", ["credited_units", "allocation"])
def test_counts_must_be_nonnegative_integers(field: str, invalid: object) -> None:
    counts = {"credited_units": 0, "allocation": 600}
    counts[field] = invalid

    with pytest.raises(ValueError, match=f"{field} must be a nonnegative integer"):
        calculate_progress(
            **counts, planning_start=START, planning_end=END, data_through=END
        )


@pytest.mark.parametrize("end", [START, START - timedelta(days=1)])
def test_period_must_have_positive_duration(end: date) -> None:
    with pytest.raises(ValueError, match="planning_end must be after planning_start"):
        calculate_progress(0, 600, START, end, END)
