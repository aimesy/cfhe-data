from __future__ import annotations

from copy import deepcopy
from itertools import permutations

import pytest

from cfhe_data.dedupe import (
    RULE_ENTITLEMENT_CARRIED,
    RULE_STRONG_LINKED,
    RULE_STRONG_RELIABLE,
    RULE_STRONG_REUSED,
    RULE_TRANSFORMED_LOCATION,
    RULE_WEAK_DUAL_LOCATION,
    RULE_WEAK_LITERAL,
    RULE_WEAK_PROJECT,
    deduplicate,
    deduplicate_entitlements,
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


def test_audit_lineage_resolves_an_intermediate_row_removed_by_a_later_rule() -> None:
    earlier = row(
        YEAR="2023",
        JURS_TRACKING_ID="N/A",
        PROJECT_NAME="River Homes",
        STREET_ADDRESS="100 Main Street",
    )
    intermediate = deepcopy(earlier)
    intermediate["YEAR"] = "2024"
    latest = deepcopy(intermediate)
    latest["YEAR"] = "2025"
    latest["JURS_TRACKING_ID"] = ""

    result = deduplicate_rows([earlier, intermediate, latest])

    assert result.removed_source_indices == {2, 3}
    assert [record.source_index for record in result.retained] == [4]
    audits = {entry.source_index: entry for entry in result.audit}
    assert audits[2].rule == RULE_WEAK_LITERAL
    assert audits[2].matched_source_indices == (3,)
    assert audits[2].retained_source_indices == (4,)
    assert audits[2].retained_report_year == 2025
    assert audits[3].matched_source_indices == (4,)
    assert audits[3].retained_source_indices == (4,)
    assert audits[3].retained_report_year == 2025


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


def _records(*raw_rows: dict[str, object]) -> tuple[PermitRecord, ...]:
    return tuple(
        PermitRecord.from_mapping(raw, source_index=index)
        for index, raw in enumerate(raw_rows, start=30)
    )


def _carried(**updates: object) -> dict[str, object]:
    """Return an entitlement row with its approval date in the date slot."""

    values: dict[str, object] = {
        "BP_ISSUE_DT1": "06/15/2022",
        "PROJECT_NAME": "Hitch Ranch",
        "APN": "511-020-024",
        "STREET_ADDRESS": "4700 Huntington Dr",
        "NO_BUILDING_PERMITS": "755",
        "BP_MOD_INCOME_NDR": "113",
        "BP_ABOVE_MOD_INCOME": "642",
    }
    values.update(updates)
    return row(**values)


def test_carried_entitlement_with_a_new_tracking_id_keeps_the_latest_report() -> None:
    records = _records(
        _carried(YEAR="2022", JURS_TRACKING_ID="ENT-2022-01"),
        _carried(YEAR="2024", JURS_TRACKING_ID="PL24-0077", PROJECT_NAME="Tract 5045"),
    )

    assert not deduplicate(records).removed
    result = deduplicate_entitlements(records)

    assert [record.source_index for record in result.retained] == [31]
    assert result.removed_source_indices == {30}
    assert result.audit[0].rule == RULE_ENTITLEMENT_CARRIED
    assert result.audit[0].retained_source_indices == (31,)
    assert result.audit[0].retained_report_year == 2024


def test_carried_entitlement_links_on_the_parcel_when_the_address_changes() -> None:
    records = _records(
        _carried(YEAR="2022", JURS_TRACKING_ID="ENT-101"),
        _carried(
            YEAR="2023", JURS_TRACKING_ID="ENT-202", STREET_ADDRESS="9 Willow Way"
        ),
        _carried(
            YEAR="2024", JURS_TRACKING_ID="ENT-303", STREET_ADDRESS="9 Willow Way"
        ),
    )

    assert not deduplicate(records).removed
    result = deduplicate_entitlements(records)

    assert [record.source_index for record in result.retained] == [32]
    assert {entry.rule for entry in result.audit} == {RULE_ENTITLEMENT_CARRIED}
    assert all(entry.retained_source_indices == (32,) for entry in result.audit)


def test_carried_entitlement_links_on_the_address_without_a_parcel() -> None:
    records = _records(
        _carried(YEAR="2022", JURS_TRACKING_ID="ENT-101", APN=""),
        _carried(
            YEAR="2024",
            JURS_TRACKING_ID="ENT-202",
            APN="",
            STREET_ADDRESS="4700 Huntington Drive",
        ),
    )

    result = deduplicate_entitlements(records)

    assert result.removed_source_indices == {30}
    assert result.audit[0].rule == RULE_ENTITLEMENT_CARRIED


@pytest.mark.parametrize(
    "changes",
    [
        {"APN": "511-020-099", "STREET_ADDRESS": "12 Oak Avenue"},
        {"BP_ISSUE_DT1": "06/16/2022"},
        {"BP_MOD_INCOME_NDR": "112", "BP_ABOVE_MOD_INCOME": "643"},
        {"NO_BUILDING_PERMITS": "756", "BP_ABOVE_MOD_INCOME": "643"},
        {"JURIS_NAME": "Other City"},
        {"YEAR": "2022"},
    ],
)
def test_carried_entitlement_requires_every_signal(changes: dict[str, object]) -> None:
    records = _records(
        _carried(YEAR="2022", JURS_TRACKING_ID="ENT-101"),
        _carried(**{"YEAR": "2024", "JURS_TRACKING_ID": "ENT-202", **changes}),
    )

    assert not deduplicate_entitlements(records).removed


def test_carried_entitlement_keeps_rows_without_an_approval_date() -> None:
    records = _records(
        _carried(YEAR="2022", JURS_TRACKING_ID="ENT-101", BP_ISSUE_DT1=""),
        _carried(YEAR="2024", JURS_TRACKING_ID="ENT-202", BP_ISSUE_DT1=""),
    )

    assert not deduplicate_entitlements(records).removed


def test_carried_entitlement_is_guarded_when_the_later_report_has_fewer_rows() -> None:
    records = _records(
        _carried(YEAR="2022", JURS_TRACKING_ID="ENT-101", STREET_ADDRESS=""),
        _carried(YEAR="2022", JURS_TRACKING_ID="ENT-102", STREET_ADDRESS=""),
        _carried(YEAR="2024", JURS_TRACKING_ID="ENT-202", STREET_ADDRESS=""),
    )

    assert not deduplicate_entitlements(records).removed


def test_carried_entitlement_guard_counts_rows_an_earlier_rule_removed() -> None:
    # Weak tracking IDs send these rows through the weak project rule, which
    # removes one 2022 row against the parcel. The other 2022 row must stay.
    records = _records(
        _carried(
            YEAR="2022",
            JURS_TRACKING_ID="A1",
            STREET_ADDRESS="4700 Huntington Dr Unit 1",
        ),
        _carried(
            YEAR="2022",
            JURS_TRACKING_ID="A2",
            STREET_ADDRESS="4700 Huntington Dr Unit 2",
        ),
        _carried(YEAR="2024", JURS_TRACKING_ID="A3", STREET_ADDRESS=""),
    )

    permit_result = deduplicate(records)
    result = deduplicate_entitlements(records)

    assert len(permit_result.removed) == 1
    assert result.removed_source_indices == permit_result.removed_source_indices


def test_carried_entitlement_resolves_through_a_row_an_earlier_rule_removed() -> None:
    records = _records(
        _carried(YEAR="2022", JURS_TRACKING_ID="A1"),
        _carried(YEAR="2023", JURS_TRACKING_ID="A2", STREET_ADDRESS="9 Willow Way"),
        _carried(
            YEAR="2024", JURS_TRACKING_ID="A3", APN="", STREET_ADDRESS="9 Willow Way"
        ),
    )

    permit_result = deduplicate(records)
    forward = deduplicate_entitlements(records)
    reversed_result = deduplicate_entitlements(tuple(reversed(records)))
    repeated = deduplicate_entitlements(forward.retained)

    assert permit_result.removed_source_indices == {31}
    assert forward.removed_source_indices == {30, 31}
    assert {entry.source_index: entry.rule for entry in forward.audit}[30] == (
        RULE_ENTITLEMENT_CARRIED
    )
    assert all(entry.retained_source_indices == (32,) for entry in forward.audit)
    assert forward.removed_source_indices == reversed_result.removed_source_indices
    assert not repeated.removed


def test_carried_entitlement_keeps_a_row_whose_lineage_would_end_in_two_years() -> None:
    # The strong linked rule moves one 2023 row toward a 2024 report with
    # other units, so the 2022 row would resolve to both 2023 and 2024.
    records = _records(
        _carried(YEAR="2022", JURS_TRACKING_ID="ENT-101"),
        _carried(YEAR="2023", JURS_TRACKING_ID="ENT-202"),
        _carried(YEAR="2023", JURS_TRACKING_ID="ENT-303"),
        _carried(
            YEAR="2024",
            JURS_TRACKING_ID="ENT-202",
            NO_BUILDING_PERMITS="700",
            BP_ABOVE_MOD_INCOME="587",
        ),
    )

    permit_result = deduplicate(records)
    result = deduplicate_entitlements(records)

    assert permit_result.removed_source_indices == {31}
    assert result.removed_source_indices == {31}
    assert result.audit[0].retained_source_indices == (33,)


DATE = "06/15/2021"
PARCEL = "111-222-333"
MOD_ONE = {"BP_ABOVE_MOD_INCOME": "0", "BP_MOD_INCOME_NDR": "1"}
LOW_ONE = {"BP_ABOVE_MOD_INCOME": "0", "BP_LOW_INCOME_NDR": "1"}


def _small(**updates: object) -> dict[str, object]:
    values: dict[str, object] = {"BP_ISSUE_DT1": DATE, "STREET_ADDRESS": ""}
    values.update(updates)
    return row(**values)


def _outcomes(records: tuple[PermitRecord, ...]) -> set[tuple[object, ...]]:
    """Return every distinct audit the carried pass gives over input orders."""

    found: set[tuple[object, ...]] = set()
    for order in permutations(records):
        result = deduplicate_entitlements(order)
        found.add(
            tuple(
                sorted(
                    (entry.source_index, entry.rule, entry.retained_source_indices)
                    for entry in result.audit
                )
            )
        )
    return found


def test_carried_entitlement_skips_a_move_that_would_split_an_earlier_lineage() -> None:
    # Strong linked removes the 2023 row toward both 2024 rows. The carried
    # pass would move only the 2024 row on the parcel, toward 2025.
    records = _records(
        _small(
            YEAR="2023", JURS_TRACKING_ID="TRK-1", APN=PARCEL, STREET_ADDRESS="5 Elm St"
        ),
        _small(YEAR="2024", JURS_TRACKING_ID="TRK-1", APN=PARCEL, **MOD_ONE),
        _small(
            YEAR="2024", JURS_TRACKING_ID="TRK-1", STREET_ADDRESS="5 Elm St", **LOW_ONE
        ),
        _small(YEAR="2025", JURS_TRACKING_ID="UUU-1", APN=PARCEL, **MOD_ONE),
    )

    assert _outcomes(records) == {((30, RULE_STRONG_LINKED, (31, 32)),)}


def test_carried_entitlement_settles_later_years_before_earlier_rows() -> None:
    records = _records(
        _small(YEAR="2022", JURS_TRACKING_ID="SRC-1", APN=PARCEL),
        _small(YEAR="2023", JURS_TRACKING_ID="TRA-1", APN=PARCEL),
        _small(YEAR="2023", JURS_TRACKING_ID="TRB-1", APN=PARCEL),
        _small(YEAR="2024", JURS_TRACKING_ID="TRA-1", APN=PARCEL, **MOD_ONE),
        _small(YEAR="2024", JURS_TRACKING_ID="TRB-1", APN=PARCEL, **LOW_ONE),
        _small(YEAR="2025", JURS_TRACKING_ID="UUU-1", APN=PARCEL, **MOD_ONE),
    )

    assert _outcomes(records) == {
        (
            (31, RULE_STRONG_LINKED, (35,)),
            (32, RULE_STRONG_LINKED, (34,)),
            (33, RULE_ENTITLEMENT_CARRIED, (35,)),
        )
    }


def test_carried_entitlement_keeps_order_independent_lineage_through_a_split() -> None:
    records = _records(
        _small(YEAR="2022", JURS_TRACKING_ID="SRC-1", APN=PARCEL),
        _small(
            YEAR="2023", JURS_TRACKING_ID="TRK-1", APN=PARCEL, STREET_ADDRESS="5 Elm St"
        ),
        _small(YEAR="2024", JURS_TRACKING_ID="TRK-1", APN=PARCEL, **MOD_ONE),
        _small(
            YEAR="2024", JURS_TRACKING_ID="TRK-1", STREET_ADDRESS="5 Elm St", **LOW_ONE
        ),
        _small(YEAR="2025", JURS_TRACKING_ID="UUU-1", APN=PARCEL, **MOD_ONE),
    )

    assert _outcomes(records) == {
        (
            (30, RULE_ENTITLEMENT_CARRIED, (32, 33)),
            (31, RULE_STRONG_LINKED, (32, 33)),
        )
    }


def test_carried_entitlement_keeps_a_row_whose_only_links_are_sibling_permits() -> None:
    # Chico, 2023 and 2024 APRs: other permits of the PROD22 series on the
    # same master parcel at other house numbers.
    shared = {
        "BP_ISSUE_DT1": "07/18/2023",
        "APN": "007430023000",
        "UNIT_CAT": "SFD",
        "TENURE": "Owner",
    }
    records = _records(
        row(
            YEAR="2023",
            JURS_TRACKING_ID="PROD22-00378",
            STREET_ADDRESS="356 Gladstone Cmns",
            **shared,
        ),
        row(
            YEAR="2024",
            JURS_TRACKING_ID="PROD22-00359",
            STREET_ADDRESS="332 Gladstone Cmns",
            **shared,
        ),
        row(
            YEAR="2024",
            JURS_TRACKING_ID="PROD22-00361",
            STREET_ADDRESS="327 Bellamy Ter",
            **shared,
        ),
    )

    assert not deduplicate(records).removed
    assert not deduplicate_entitlements(records).removed


def test_carried_entitlement_links_through_an_intermediate_report() -> None:
    # The 2022 row shares only the address with 2023, and 2023 shares only
    # the parcel with 2024; the component still joins all three.
    records = _records(
        _carried(
            YEAR="2022", JURS_TRACKING_ID="ENT-101", APN="", STREET_ADDRESS="5 Elm St"
        ),
        _carried(YEAR="2023", JURS_TRACKING_ID="ENT-202", STREET_ADDRESS="5 Elm St"),
        _carried(
            YEAR="2024", JURS_TRACKING_ID="ENT-303", STREET_ADDRESS="9 Willow Way"
        ),
    )

    result = deduplicate_entitlements(records)

    assert result.removed_source_indices == {30, 31}
    assert all(entry.rule == RULE_ENTITLEMENT_CARRIED for entry in result.audit)


def test_carried_entitlement_ignores_a_unit_conflict_on_the_parcel() -> None:
    records = _records(
        _carried(
            YEAR="2023", JURS_TRACKING_ID="BCB-1", STREET_ADDRESS="10 Oak Ave Unit B"
        ),
        _carried(
            YEAR="2024", JURS_TRACKING_ID="BCB-1", STREET_ADDRESS="10 Oak Ave Unit C"
        ),
    )

    result = deduplicate_entitlements(records)

    assert result.removed_source_indices == {30}
    assert result.audit[0].rule == RULE_ENTITLEMENT_CARRIED


@pytest.mark.parametrize("changes", [{"TENURE": "Owner"}, {"UNIT_CAT": "SFD"}])
def test_carried_entitlement_ignores_unit_category_and_tenure(
    changes: dict[str, object],
) -> None:
    records = _records(
        _carried(YEAR="2022", JURS_TRACKING_ID="ENT-101"),
        _carried(**{"YEAR": "2024", "JURS_TRACKING_ID": "ENT-202", **changes}),
    )

    result = deduplicate_entitlements(records)

    assert result.removed_source_indices == {30}
    assert result.audit[0].rule == RULE_ENTITLEMENT_CARRIED


def test_carried_entitlement_lone_parcel_row_still_overrides_the_address() -> None:
    # San Fernando 2022 and 2023 APRs: one ADU per year on the parcel, house
    # number off by two, no permit date on the earlier row.
    shared = {"BP_ISSUE_DT1": "12/16/2022", "APN": "2515-003-003", "UNIT_CAT": "ADU"}
    records = _records(
        row(
            YEAR="2022",
            JURS_TRACKING_ID="B22-0101",
            STREET_ADDRESS="657 Newton St.",
            **shared,
        ),
        row(
            YEAR="2023",
            JURS_TRACKING_ID="B23-0442",
            STREET_ADDRESS="659 Newton Street",
            **shared,
        ),
    )

    result = deduplicate_entitlements(records)

    assert result.removed_source_indices == {30}
    assert result.audit[0].rule == RULE_ENTITLEMENT_CARRIED
