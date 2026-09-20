"""Pace plans preserve counts and fail closed on incomplete live evidence.

visibility: public
"""

import copy

import pytest

from cfhe_data import pace_review as p
from cfhe_data.webflow import canonical_sha256


@pytest.fixture
def inputs():
    policy = {
        "policy_version": 1,
        "default_cycle": "6th",
        "expected_jurisdiction_count": 2,
        "expected_cycle_counts": {"6th": 1, "7th": 1},
        "overrides": [],
        "source": "https://www.hcd.ca.gov/rhna/seventh-cycle",
    }
    source = {
        "metadata": {
            "cutoff_year": 2025,
            "last_updated": "2026-09-19",
            "provisional": False,
            "complete_after": "2026-06-30",
            "source_manifest_sha256": "b" * 64,
            "dedupe_profile": "test-profile",
            "cycle_policy_sha256": canonical_sha256(policy),
            "cycle_counts": {"6th": 1, "7th": 1},
            "jurisdiction_count": 2,
            "selected_row_count": 2,
            "selected_units": 300,
            "retained_row_count": 2,
            "removed_row_count": 0,
            "removed_units": 0,
            "undated_permits": 0,
        },
        "jurisdictions": [
            {
                "jurisdiction": "Example City",
                "jurisdiction_key": "EXAMPLE CITY",
                "cycle": "6th",
                "period_start": "2021-12-31",
                "period_end": "2029-12-31",
                "acutely_low": 0,
                "extremely_low": 10,
                "very_low": 40,
                "low": 50,
                "moderate": 50,
                "above_moderate": 150,
                "total": 300,
                "undated_permits": 0,
                "last_updated": "2026-09-19",
                "data_status": "reported",
            }
        ],
    }
    fields = {
        p.CITY_LINK: ["recCity"],
        p.CYCLE: ["6th"],
        p.CURRENT: 1,
        p.CORRECT_LINK: 1,
        p.TOTAL: 300,
        p.ALLOCATION: 600,
        p.PROGRESS_OVERRIDE: 0,
        p.TIME_REMAINING: 0.4,
        p.STATUS: "Off Target",
        **dict(zip(p.PERMITS.values(), (50, 50, 50, 150), strict=True)),
        **dict(zip(p.ALLOCATIONS.values(), (100, 100, 100, 300), strict=True)),
    }
    types = {
        p.CITY_LINK: "multipleRecordLinks",
        p.CYCLE: "multipleLookupValues",
        p.CURRENT: "formula",
        p.CORRECT_LINK: "formula",
        p.TOTAL: "formula",
        p.ALLOCATION: "formula",
        p.PROGRESS_OVERRIDE: "number",
        p.TIME_REMAINING: "formula",
        p.STATUS: "formula",
        **{value: "number" for value in (*p.PERMITS.values(), *p.ALLOCATIONS.values())},
    }
    schema = {
        "tables": [
            {
                "id": p.RHNA,
                "fields": [
                    {
                        "id": fid,
                        "name": fid,
                        "type": typ,
                        "options": {"isValid": True, "formula": "OLD_EXPRESSION"},
                    }
                    for fid, typ in types.items()
                ],
            }
        ]
    }
    snapshot = {
        "base_id": p.BASE_ID,
        "complete": True,
        "terminal_offset_reached": True,
        "rhna_scope": "current",
        "observed_at": "2026-09-19T23:00:00+00:00",
        "records_by_table": {
            p.JURISDICTIONS: [
                {"id": "recCity", "fields": {p.NAME: "Example City"}},
                {"id": "recDummy", "fields": {p.NAME: "Sitekick City"}},
            ],
            p.RHNA: [{"id": "recRhna", "fields": fields}],
        },
    }
    second = copy.deepcopy(source["jurisdictions"][0])
    second.update(
        jurisdiction="Other City",
        jurisdiction_key="OTHER CITY",
        cycle="7th",
        period_start="2024-06-30",
        period_end="2029-06-30",
    )
    for key in (
        "extremely_low",
        "very_low",
        "low",
        "moderate",
        "above_moderate",
        "total",
    ):
        second[key] = 0
    source["jurisdictions"].append(second)
    snapshot["records_by_table"][p.JURISDICTIONS].append(
        {"id": "recOther", "fields": {p.NAME: "Other City"}}
    )
    second_fields = copy.deepcopy(fields)
    second_fields.update({p.CITY_LINK: ["recOther"], p.CYCLE: ["7th"], p.TOTAL: 0})
    for fid in p.PERMITS.values():
        second_fields[fid] = 0
    snapshot["records_by_table"][p.RHNA].append(
        {"id": "recOtherRhna", "fields": second_fields}
    )
    return {
        "totals": source,
        "cycle_policy": policy,
        "schema": schema,
        "snapshot": snapshot,
    }


def test_halfway_equality_changes_pace_without_changing_counts(inputs):
    original = copy.deepcopy(inputs)
    result = p.build_pace_review(**inputs)
    assert inputs == original
    assert result["coverage_complete"] and result["ready_for_schema_migration_review"]
    assert result["count_changes"] == 0
    row = result["comparisons"][0]
    assert row["before_status"] == "Off Target"
    assert row["after_status"] == "On Target"
    assert row["pace"]["minimum_units_for_pace"] == 300
    assert row["pace"]["elapsed_fraction"] == 0.5
    assert row["permit_values"]["vli"] == 50
    assert row["data_status"] == "reported"
    assert row["pace_by_income"]["vli"]["on_pace"] is True
    assert result["status_change_count"] == 1
    assert result["proposed_metadata_updates"][0]["proposed_fields_by_name"] == {
        "APR Data Through": "2025-12-31",
        "APR Planning Period Start": "2021-12-31",
        "APR Planning Period End": "2029-12-31",
    }
    updates = result["schema_migration"]["formula_updates"]
    assert [row["field_id"] for row in updates] == [p.TIME_REMAINING, p.STATUS]
    for update in updates:
        assert update["proposed_formula"].endswith(",OLD_EXPRESSION)")
        assert update["expected_formula"] == "OLD_EXPRESSION"
    assert ">=" in updates[1]["proposed_formula"]
    assert result["source_totals_sha256"] == canonical_sha256(inputs["totals"])
    assert result["snapshot_sha256"] == canonical_sha256(inputs["snapshot"])


@pytest.mark.parametrize("field", [p.ALLOCATION, p.ALLOCATIONS["vli"]])
def test_missing_allocation_remains_unknown_and_blocks_readiness(inputs, field):
    del inputs["snapshot"]["records_by_table"][p.RHNA][0]["fields"][field]
    result = p.build_pace_review(**inputs)
    assert not result["ready_for_schema_migration_review"]
    assert len(result["unknown_allocations"]) == 1
    row = result["comparisons"][0]
    if field == p.ALLOCATION:
        assert row["after_status"] is None
        assert row["pace"]["status"] == "unknown_allocation"
        assert not row["status_changed"]
        assert result["after_status_counts"]["Unknown"] == 1
    else:
        assert row["pace_by_income"]["vli"]["status"] == "unknown_allocation"


def test_zero_band_allocation_is_not_missing(inputs):
    fields = inputs["snapshot"]["records_by_table"][p.RHNA][0]["fields"]
    fields[p.ALLOCATION] -= fields[p.ALLOCATIONS["vli"]]
    fields[p.ALLOCATIONS["vli"]] = 0
    result = p.build_pace_review(**inputs)
    assert not result["unknown_allocations"]
    assert (
        result["comparisons"][0]["pace_by_income"]["vli"]["status"] == "no_allocation"
    )


def test_zero_total_allocation_is_known_and_clears_previous_status(inputs):
    fields = inputs["snapshot"]["records_by_table"][p.RHNA][0]["fields"]
    for field in (p.ALLOCATION, *p.ALLOCATIONS.values()):
        fields[field] = 0
    result = p.build_pace_review(**inputs)
    row = result["comparisons"][0]
    assert result["ready_for_schema_migration_review"]
    assert not result["unknown_allocations"]
    assert row["pace"]["status"] == "no_allocation"
    assert row["pace"]["on_pace"] is None
    assert row["before_expected_units"] == 0
    assert row["after_status"] is None
    assert row["status_changed"]
    assert result["status_change_count"] == 1
    assert result["after_status_counts"]["No pace judgment"] == 1
    assert "Unknown" not in result["after_status_counts"]


def test_unstarted_period_counts_the_transition_to_blank(inputs):
    inputs["totals"]["jurisdictions"][0]["period_start"] = "2025-12-31"
    result = p.build_pace_review(**inputs)
    row = result["comparisons"][0]
    assert row["pace"]["status"] == "not_started"
    assert row["after_status"] is None
    assert row["status_changed"]
    assert result["status_change_count"] == 1


def test_existing_blank_and_no_selected_rows_provenance(inputs):
    inputs["totals"]["jurisdictions"][1]["data_status"] = "no_selected_rows"
    f = inputs["snapshot"]["records_by_table"][p.RHNA][1]["fields"]
    for field in (p.ALLOCATION, *p.ALLOCATIONS.values()):
        f[field] = 0
    f[p.STATUS] = ""
    result = p.build_pace_review(**inputs)
    row = result["comparisons"][1]
    assert row["data_status"] == "no_selected_rows"
    assert row["after_status"] is None
    assert not row["status_changed"]


def test_allocation_total_must_match_income_bands(inputs):
    inputs["snapshot"]["records_by_table"][p.RHNA][0]["fields"][p.ALLOCATION] += 1
    with pytest.raises(ValueError, match="allocations do not reconcile"):
        p.build_pace_review(**inputs)


def test_explicit_baseline_reviews_new_counts_and_dates_against_old_live_values(inputs):
    baseline = copy.deepcopy(inputs["totals"])
    proposed = inputs["totals"]["jurisdictions"][0]
    proposed["above_moderate"] += 10
    proposed["total"] += 10
    proposed["period_start"] = "2021-12-01"
    inputs["totals"]["metadata"]["selected_units"] += 10
    with pytest.raises(ValueError, match="Live permits disagree"):
        p.build_pace_review(**inputs)
    result = p.build_pace_review(**inputs, baseline_totals=baseline)
    row = result["comparisons"][0]
    assert row["before_total"] == 300
    assert row["after_total"] == row["total"] == 310
    assert row["count_change"] == 10
    assert row["counts_changed"]
    assert row["before_permit_values"]["ami"] == 150
    assert row["after_permit_values"]["ami"] == 160
    assert row["permit_value_difference"]["ami"] == 10
    assert row["before_planning_start"] == "2021-12-31"
    assert row["planning_start"] == "2021-12-01"
    assert row["pace"]["progress_fraction"] == 310 / 600
    assert result["count_changes"] == 1
    assert result["total_count_change"] == 10
    assert result["source_totals_sha256"] != result["baseline_totals_sha256"]
    assert result["baseline_totals_sha256"] == canonical_sha256(baseline)
    stage = result["proposed_metadata_updates"][0]
    assert stage["expected_existing_fields"][p.PERMITS["ami"]] == 150
    assert stage["proposed_permit_values_by_field_id"] == {p.PERMITS["ami"]: 160}


def test_income_band_changes_count_even_when_jurisdiction_total_does_not(inputs):
    baseline = copy.deepcopy(inputs["totals"])
    row = inputs["totals"]["jurisdictions"][0]
    row["low"] += 10
    row["above_moderate"] -= 10
    result = p.build_pace_review(**inputs, baseline_totals=baseline)
    assert result["count_changes"] == 1
    assert result["total_count_change"] == 0
    assert result["comparisons"][0]["count_change"] == 0
    assert result["comparisons"][0]["counts_changed"]


@pytest.mark.parametrize("change", ["cutoff", "cycles", "coverage", "live"])
def test_combined_review_rejects_unrelated_baseline_drift(inputs, change):
    baseline = copy.deepcopy(inputs["totals"])
    if change == "cutoff":
        baseline["metadata"].update(cutoff_year=2024, complete_after="2025-06-30")
    elif change == "cycles":
        baseline["jurisdictions"][0]["cycle"] = "7th"
        baseline["jurisdictions"][1]["cycle"] = "6th"
    elif change == "coverage":
        baseline["jurisdictions"][1].update(
            jurisdiction="Other Place", jurisdiction_key="OTHER PLACE"
        )
    else:
        inputs["snapshot"]["records_by_table"][p.RHNA][0]["fields"][
            p.PERMITS["vli"]
        ] += 1
    with pytest.raises(ValueError):
        p.build_pace_review(**inputs, baseline_totals=baseline)


def test_existing_date_fields_resolve_ids_and_preserve_expected_values(inputs):
    for logical, name in p.METADATA_FIELDS.items():
        inputs["schema"]["tables"][0]["fields"].append(
            {"id": f"fld_{logical}", "name": name, "type": "date"}
        )
    inputs["snapshot"]["records_by_table"][p.RHNA][0]["fields"]["fld_data_through"] = (
        "2024-12-31"
    )
    result = p.build_pace_review(**inputs)
    migration = result["schema_migration"]
    assert migration["new_fields"] == []
    assert migration["resolved_metadata_field_ids"] == {
        name: f"fld_{name}" for name in p.METADATA_FIELDS
    }
    assert "{fld_data_through}" in migration["formula_updates"][0]["proposed_formula"]
    stage = result["proposed_metadata_updates"][0]
    assert stage["expected_existing_fields"]["fld_data_through"] == "2024-12-31"
    assert stage["expected_existing_fields"]["fld_period_start"] is None


def test_existing_metadata_field_with_wrong_type_blocks_review(inputs):
    inputs["schema"]["tables"][0]["fields"].append(
        {"id": "fld_date", "name": "APR Data Through", "type": "singleLineText"}
    )
    with pytest.raises(ValueError, match="one date field"):
        p.build_pace_review(**inputs)


@pytest.mark.parametrize(
    "change",
    [
        "missing_city",
        "duplicate_rhna",
        "missing_rhna",
        "wrong_cycle",
        "wrong_permits",
        "override",
        "wrong_schema",
        "not_current",
        "bad_link",
        "policy",
        "pagination",
        "source_coverage",
        "unincorporated",
        "missing_permit",
    ],
)
def test_unreliable_evidence_cannot_create_a_pace_plan(inputs, change):
    snap = inputs["snapshot"]
    f = snap["records_by_table"][p.RHNA][0]["fields"]
    if change == "missing_city":
        snap["records_by_table"][p.JURISDICTIONS].pop()
    elif change == "duplicate_rhna":
        snap["records_by_table"][p.RHNA].append(
            copy.deepcopy(snap["records_by_table"][p.RHNA][0])
        )
    elif change == "missing_rhna":
        snap["records_by_table"][p.RHNA] = []
    elif change == "wrong_cycle":
        f[p.CYCLE] = ["7th"]
    elif change == "wrong_permits":
        f[p.PERMITS["vli"]] += 1
    elif change == "override":
        f[p.PROGRESS_OVERRIDE] = 1
    elif change == "wrong_schema":
        inputs["schema"]["tables"][0]["fields"][0]["type"] = "number"
    elif change == "not_current":
        f[p.CURRENT] = 0
    elif change == "bad_link":
        f[p.CORRECT_LINK] = 0
    elif change == "policy":
        inputs["cycle_policy"]["source"] = "changed"
    elif change == "pagination":
        snap["terminal_offset_reached"] = False
    elif change == "source_coverage":
        inputs["cycle_policy"]["expected_jurisdiction_count"] = 539
    elif change == "unincorporated":
        snap["records_by_table"][p.JURISDICTIONS][0]["fields"][p.UNINCORPORATED] = True
    else:
        del f[p.PERMITS["vli"]]
    with pytest.raises(ValueError):
        p.build_pace_review(**inputs)
