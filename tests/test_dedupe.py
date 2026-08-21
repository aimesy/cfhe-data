from __future__ import annotations

from copy import deepcopy

from cfhe_data.dedupe import (
    RULE_STRONG_LINKED,
    RULE_STRONG_RELIABLE,
    RULE_STRONG_REUSED,
    RULE_TRANSFORMED_LOCATION,
    RULE_WEAK_DUAL_LOCATION,
    RULE_WEAK_LITERAL,
    RULE_WEAK_PROJECT,
    deduplicate,
    deduplicate_rows,
    house_number_moved_from_apn_to_address,
)
from cfhe_data.models import DEFAULT_CATEGORY_FIELDS, PermitRecord


def row(**updates: object) -> dict[str, object]:
    result: dict[str, object] = {
        "YEAR": "2024",
        "JURIS_NAME": "Example City",
        "BP_ISSUE_DT1": "01/15/2024",
        "JURS_TRACKING_ID": "BP-100",
        "PROJECT_NAME": "",
        "APN": "",
        "STREET_ADDRESS": "100 Main Street",
        "STD_ADDRESS": "",
        "LATITUDE": "",
        "LONGITUDE": "",
        "NO_BUILDING_PERMITS": "1",
        "UNIT_CAT": "5+ Units",
        "TENURE": "Rental",
    }
    for group in DEFAULT_CATEGORY_FIELDS:
        for field in group:
            result[field] = "0"
    result["BP_ABOVE_MOD_INCOME"] = "1"
    result.update(updates)
    return result


def test_spelling_and_suffix_tolerance_remove_only_earlier_snapshot() -> None:
    earlier = row(STREET_ADDRESS="100 North Main Street")
    latest = row(YEAR="2025", STREET_ADDRESS="100 N Mainn St")

    result = deduplicate_rows([earlier, latest], source_index_start=10)

    assert [record.source_index for record in result.retained] == [11]
    assert result.removed_source_indices == {10}
    assert result.audit[0].rule == RULE_STRONG_LINKED


def test_different_house_unit_and_street_remain_ambiguous() -> None:
    earlier = row(STREET_ADDRESS="100 Main Street Apt 2")
    latest = row(YEAR="2025", STREET_ADDRESS="101 Oak Avenue Apt 3")

    result = deduplicate_rows([earlier, latest])

    assert len(result.retained) == 2
    assert not result.audit


def test_reused_tracking_id_is_guarded_without_dual_corroboration() -> None:
    records = [
        row(STREET_ADDRESS="100 Main Street"),
        row(YEAR="2025", STREET_ADDRESS="900 Other Road"),
        row(
            YEAR="2025",
            BP_ISSUE_DT1="02/20/2024",
            STREET_ADDRESS="300 Third Street",
        ),
    ]

    result = deduplicate_rows(records)

    assert len(result.retained) == 3
    assert not result.audit


def test_reused_tracking_id_exception_requires_apn_and_coordinates() -> None:
    records = [
        row(
            STREET_ADDRESS="100 Main Street",
            APN="123-456",
            LATITUDE="38.55",
            LONGITUDE="-121.45",
        ),
        row(
            YEAR="2025",
            STREET_ADDRESS="900 Other Road",
            APN="123-456",
            LATITUDE="38.55",
            LONGITUDE="-121.45",
        ),
        row(
            YEAR="2025",
            BP_ISSUE_DT1="02/20/2024",
            STREET_ADDRESS="300 Third Street",
        ),
    ]

    result = deduplicate_rows(records)

    assert result.removed_source_indices == {2}
    assert result.audit[0].rule == RULE_STRONG_REUSED


def test_component_safety_rejects_an_incompatible_bridge() -> None:
    records = [
        row(YEAR="2023", STREET_ADDRESS="100 Main St"),
        row(YEAR="2024", STREET_ADDRESS="100 Maix St"),
        row(YEAR="2025", STREET_ADDRESS="100 Mazx St"),
        row(
            YEAR="2025",
            BP_ISSUE_DT1="02/20/2024",
            STREET_ADDRESS="300 Third Street",
        ),
    ]

    result = deduplicate_rows(records)

    assert result.removed_source_indices == {2}
    assert [record.source_index for record in result.retained] == [3, 4, 5]
    assert result.audit[0].rule == RULE_STRONG_LINKED


def test_every_latest_same_year_row_is_preserved() -> None:
    earlier = row()
    latest_one = row(YEAR="2025")
    latest_two = deepcopy(latest_one)

    result = deduplicate_rows([earlier, latest_one, latest_two])

    assert [record.source_index for record in result.retained] == [3, 4]
    assert result.removed_source_indices == {2}
    assert result.audit[0].retained_source_indices == (3, 4)


def test_weak_literal_group_is_guarded_when_latest_count_is_smaller() -> None:
    earlier_one = row(JURS_TRACKING_ID="N/A", PROJECT_NAME="")
    earlier_two = deepcopy(earlier_one)
    latest = deepcopy(earlier_one)
    latest["YEAR"] = "2025"

    result = deduplicate_rows([earlier_one, earlier_two, latest])

    assert len(result.retained) == 3
    assert RULE_WEAK_LITERAL not in result.counts_by_rule()


def test_strong_residual_rule_has_a_named_row_audit() -> None:
    earlier = row(
        STREET_ADDRESS="100 Main Street",
        LATITUDE="38.55",
        LONGITUDE="-121.45",
    )
    latest = row(
        YEAR="2025",
        STREET_ADDRESS="900 Other Road",
        LATITUDE="38.55",
        LONGITUDE="-121.45",
    )

    result = deduplicate_rows([earlier, latest])

    assert result.removed_source_indices == {2}
    assert result.audit[0].rule == RULE_STRONG_RELIABLE
    assert ("corroboration", "exact_valid_coordinates") in result.audit[0].details


def test_strong_residual_rule_does_not_remove_on_house_number_alone() -> None:
    earlier = row(
        PROJECT_NAME="",
        APN="",
        STREET_ADDRESS="100 Main Street",
        STD_ADDRESS="",
        LATITUDE="",
        LONGITUDE="",
    )
    latest = row(
        YEAR="2025",
        PROJECT_NAME="",
        APN="",
        STREET_ADDRESS="100 Oak Road",
        STD_ADDRESS="",
        LATITUDE="",
        LONGITUDE="",
    )

    result = deduplicate_rows([earlier, latest])

    assert len(result.retained) == 2
    assert not result.removed
    assert not result.audit


def test_strong_residual_rule_vetoes_conflicting_units() -> None:
    earlier = row(
        STREET_ADDRESS="100 Main Street Apt 2",
        APN="123-456",
        LATITUDE="38.55",
        LONGITUDE="-121.45",
    )
    latest = row(
        YEAR="2025",
        STREET_ADDRESS="100 Main Street Apt 3",
        APN="123-456",
        LATITUDE="38.55",
        LONGITUDE="-121.45",
    )

    result = deduplicate_rows([earlier, latest])

    assert len(result.retained) == 2
    assert not result.removed
    assert not result.audit


def test_strong_residual_evidence_is_bound_to_each_removed_row() -> None:
    earlier_matched = row(
        STREET_ADDRESS="100 Main Street",
        APN="123-456",
        LATITUDE="38.55",
        LONGITUDE="-121.45",
    )
    earlier_blank = row(STREET_ADDRESS="", APN="")
    latest_matched = row(
        YEAR="2025",
        STREET_ADDRESS="900 Other Road",
        APN="123-456",
        LATITUDE="38.55",
        LONGITUDE="-121.45",
    )
    latest_blank = row(YEAR="2025", STREET_ADDRESS="", APN="")

    result = deduplicate_rows(
        [earlier_matched, earlier_blank, latest_matched, latest_blank]
    )

    assert result.removed_source_indices == {2}
    assert [record.source_index for record in result.retained] == [3, 4, 5]
    assert result.audit[0].retained_source_indices == (4,)
    assert (
        "corroboration",
        "exact_valid_apn,exact_valid_coordinates",
    ) in result.audit[0].details


def test_weak_project_rule_requires_matching_metadata_and_location() -> None:
    earlier = row(
        JURS_TRACKING_ID="N/A",
        PROJECT_NAME="River Homes",
        STREET_ADDRESS="100 Main Street",
    )
    latest = row(
        YEAR="2025",
        JURS_TRACKING_ID="",
        PROJECT_NAME="River Homes",
        STREET_ADDRESS="100 Mainn St",
    )

    result = deduplicate_rows([earlier, latest])

    assert result.removed_source_indices == {2}
    assert result.audit[0].rule == RULE_WEAK_PROJECT


def test_weak_dual_location_rule_accepts_blank_project_only_with_both_signals() -> None:
    earlier = row(
        JURS_TRACKING_ID="N/A",
        PROJECT_NAME="",
        APN="123-456",
        STREET_ADDRESS="100 Main Street",
    )
    latest = row(
        YEAR="2025",
        JURS_TRACKING_ID="",
        PROJECT_NAME="",
        APN="123-456",
        STREET_ADDRESS="100 Mainn St",
    )

    result = deduplicate_rows([earlier, latest])

    assert result.removed_source_indices == {2}
    assert result.audit[0].rule == RULE_WEAK_DUAL_LOCATION


def test_transformed_location_predicate_and_rule() -> None:
    earlier_row = row(
        JURS_TRACKING_ID="N/A",
        PROJECT_NAME="River Apartments",
        APN="123",
        STREET_ADDRESS="Main Street",
    )
    latest_row = row(
        YEAR="2025",
        JURS_TRACKING_ID="",
        PROJECT_NAME="River Apartments",
        APN="456-789",
        STREET_ADDRESS="123 Main St",
    )
    earlier = PermitRecord.from_mapping(earlier_row, source_index=2)
    latest = PermitRecord.from_mapping(latest_row, source_index=3)

    assert house_number_moved_from_apn_to_address(earlier, latest)

    result = deduplicate_rows([earlier_row, latest_row])

    assert result.removed_source_indices == {2}
    assert result.audit[0].rule == RULE_TRANSFORMED_LOCATION


def test_undated_rows_and_physical_lineage_are_preserved() -> None:
    earlier_row = row(JURS_TRACKING_ID="N/A", BP_ISSUE_DT1="")
    latest_row = deepcopy(earlier_row)
    latest_row["YEAR"] = "2025"

    result = deduplicate_rows(
        [earlier_row, latest_row],
        csv_physical_lines=[14, 19],
    )

    assert len(result.retained) == 2
    assert all(record.undated for record in result.retained)
    assert [record.csv_physical_line for record in result.retained] == [14, 19]


def test_deduplication_is_permutation_invariant_and_idempotent() -> None:
    raw_records = [
        row(YEAR="2023", STREET_ADDRESS="100 Main St"),
        row(YEAR="2024", STREET_ADDRESS="100 Maix St"),
        row(YEAR="2025", STREET_ADDRESS="100 Mazx St"),
    ]
    records = tuple(
        PermitRecord.from_mapping(raw, source_index=index)
        for index, raw in enumerate(raw_records, start=20)
    )

    forward = deduplicate(records)
    reversed_result = deduplicate(tuple(reversed(records)))
    repeated = deduplicate(forward.retained)

    assert forward.removed_source_indices == reversed_result.removed_source_indices
    assert not repeated.removed
    assert not repeated.audit
