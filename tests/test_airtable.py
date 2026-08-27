from __future__ import annotations

from copy import deepcopy

import pytest

from cfhe_data.airtable import (
    AirtableCoverageError,
    AirtableMappingError,
    AirtableSnapshotError,
    AirtableStateChangedError,
    build_airtable_change_plan,
    verify_airtable_change_plan,
)


def jurisdiction(name: str, start: int, *, undated: int = 0) -> dict[str, object]:
    categories = {
        field: start + offset
        for offset, field in enumerate(
            (
                "acutely_low",
                "extremely_low",
                "very_low",
                "low",
                "moderate",
                "above_moderate",
            )
        )
    }
    return {
        "jurisdiction": name,
        "jurisdiction_key": name.upper(),
        **categories,
        "total": sum(categories.values()),
        "undated_permits": undated,
        "last_updated": "2026-08-21",
        "data_status": "reported",
    }


def totals() -> dict[str, object]:
    return {
        "metadata": {
            "cutoff_year": 2025,
            "source_manifest_sha256": "a" * 64,
            "dedupe_profile": "audited-apr-snapshots-v3",
            "provisional": False,
            "complete_after": "2026-06-30",
            "jurisdiction_count": 2,
            "selected_row_count": 3,
            "selected_units": 109,
            "retained_row_count": 2,
            "removed_row_count": 1,
            "removed_units": 7,
            "undated_permits": 2,
            "last_updated": "2026-08-21",
        },
        "jurisdictions": [
            jurisdiction("Alpha", 1),
            jurisdiction("Beta (Unincorporated)", 11, undated=2),
        ],
    }


def rhna_record(
    record_id: str,
    jurisdiction_id: str,
    cycle: str,
    *,
    current: bool,
    vli: int,
    li: int,
    mi: int,
    ami: int,
) -> dict[str, object]:
    return {
        "record_id": record_id,
        "jurisdiction_record_ids": [jurisdiction_id],
        "cycle": cycle,
        "current": current,
        "correct_link": True,
        "vli": vli,
        "li": li,
        "mi": mi,
        "ami": ami,
        "rhna_start": "2023-01-01",
        "rhna_end": "2031-12-31",
        "total_progress_override": None,
    }


def snapshot() -> dict[str, object]:
    rhna_records = [
        rhna_record(
            "rec-alpha-6",
            "rec-alpha",
            "6th",
            current=True,
            vli=6,
            li=4,
            mi=5,
            ami=6,
        ),
        rhna_record(
            "rec-beta-6",
            "rec-beta",
            "6th",
            current=False,
            vli=35,
            li=14,
            mi=15,
            ami=16,
        ),
        rhna_record(
            "rec-beta-7",
            "rec-beta",
            "7th",
            current=True,
            vli=0,
            li=0,
            mi=0,
            ami=0,
        ),
    ]
    return {
        "snapshot_version": 1,
        "complete": True,
        "retrieved_at": "2026-08-22T12:00:00Z",
        "base_id": "app-live-test",
        "jurisdictions_table_id": "tbl-jurisdictions-test",
        "rhna_table_id": "tbl-rhna-test",
        "record_counts": {"jurisdictions": 3, "rhna_records": len(rhna_records)},
        "jurisdictions": [
            {"record_id": "rec-alpha", "name": "Alpha", "unincorporated": False},
            {
                "record_id": "rec-beta",
                "name": "Beta (Unincorporated Areas)",
                "unincorporated": True,
            },
            {
                "record_id": "rec-sitekick",
                "name": "Sitekick City",
                "unincorporated": False,
            },
        ],
        "rhna_records": rhna_records,
    }


def mapping() -> dict[str, object]:
    return {
        "base_id": "app-live-test",
        "jurisdictions_table_id": "tbl-jurisdictions-test",
        "rhna_table_id": "tbl-rhna-test",
        "comparison_cycle": "6th",
        "excluded_jurisdictions": ["Sitekick City"],
        "jurisdiction_mappings": {
            "Beta (Unincorporated)": "Beta (Unincorporated Areas)"
        },
    }


def test_plan_collapses_vli_and_preserves_unmapped_quality() -> None:
    plan = build_airtable_change_plan(totals(), snapshot(), mapping())

    assert plan["intent"] == "comparison_only"
    assert plan["apply_eligible"] is False
    assert plan["matched_count"] == 2
    assert plan["change_count"] == 1
    assert plan["unchanged_count"] == 1
    assert plan["airtable_aggregate_before"]["total"] == 101
    assert plan["source_aggregate_after"]["total"] == 102
    change = plan["changes"][0]
    assert change["jurisdiction"] == "Beta (Unincorporated)"
    assert change["changed_fields"] == ["vli"]
    assert change["fields"]["vli"] == {"before": 35, "after": 36, "delta": 1}
    assert change["unmapped_quality"] == {
        "data_status": "reported",
        "undated_permits": 2,
    }
    assert plan["current_scope"]["cycle_counts"] == {"6th": 1, "7th": 1}
    assert plan["current_scope"]["blocked_jurisdiction_count"] == 1
    verify_airtable_change_plan(plan, snapshot())


def test_exact_alias_is_required_without_fuzzy_matching() -> None:
    config = mapping()
    config["jurisdiction_mappings"] = {}

    with pytest.raises(AirtableMappingError, match="No exact Airtable"):
        build_airtable_change_plan(totals(), snapshot(), config)


def test_selected_cycle_rejects_duplicate_jurisdiction_rows() -> None:
    current = snapshot()
    duplicate = deepcopy(current["rhna_records"][1])
    duplicate["record_id"] = "rec-beta-6-duplicate"
    current["rhna_records"].append(duplicate)
    current["record_counts"]["rhna_records"] += 1

    with pytest.raises(AirtableCoverageError, match="duplicate RHNA rows"):
        build_airtable_change_plan(totals(), current, mapping())


def test_selected_cycle_requires_exactly_one_city_link() -> None:
    current = snapshot()
    current["rhna_records"][1]["jurisdiction_record_ids"] = [
        "rec-alpha",
        "rec-beta",
    ]

    with pytest.raises(AirtableCoverageError, match="exactly one jurisdiction"):
        build_airtable_change_plan(totals(), current, mapping())


def test_selected_cycle_requires_correct_link() -> None:
    current = snapshot()
    current["rhna_records"][1]["correct_link"] = False

    with pytest.raises(AirtableCoverageError, match=r"Correct Link\?"):
        build_airtable_change_plan(totals(), current, mapping())


def test_progress_override_is_a_plan_blocker() -> None:
    current = snapshot()
    current["rhna_records"][1]["total_progress_override"] = 80
    plan = build_airtable_change_plan(totals(), current, mapping())

    codes = {blocker["code"] for blocker in plan["blockers"]}
    assert "airtable_progress_overrides_present" in codes


def test_missing_current_record_is_reported() -> None:
    current = snapshot()
    current["rhna_records"][2]["current"] = False
    plan = build_airtable_change_plan(totals(), current, mapping())

    assert plan["current_scope"]["missing_jurisdictions"] == ["Beta (Unincorporated)"]
    assert plan["current_scope"]["blocked_jurisdiction_count"] == 1


def test_plan_and_snapshot_digests_ignore_input_order() -> None:
    first = build_airtable_change_plan(totals(), snapshot(), mapping())
    reordered = snapshot()
    reordered["jurisdictions"].reverse()
    reordered["rhna_records"].reverse()
    second = build_airtable_change_plan(totals(), reordered, mapping())

    assert first == second


def test_snapshot_mutation_invalidates_reviewed_plan() -> None:
    original = snapshot()
    plan = build_airtable_change_plan(totals(), original, mapping())
    changed = deepcopy(original)
    changed["rhna_records"][0]["ami"] += 1

    with pytest.raises(AirtableStateChangedError, match="Airtable state changed"):
        verify_airtable_change_plan(plan, changed)


def test_snapshot_rejects_negative_permit_values() -> None:
    current = snapshot()
    current["rhna_records"][0]["vli"] = -1

    with pytest.raises(AirtableSnapshotError, match="nonnegative integer"):
        build_airtable_change_plan(totals(), current, mapping())
