from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from cfhe_data.airtable_sync import (
    AirtableSyncBlockedError,
    AirtableSyncConfigurationError,
    AirtableSyncSnapshotError,
    AirtableSyncStateChangedError,
    build_airtable_sync_plan,
    plan_record_updates,
    project_airtable_snapshot,
    summarize_airtable_sync_plan,
    verify_airtable_sync_plan,
)
from cfhe_data.webflow import canonical_sha256

ROOT = Path(__file__).resolve().parents[1]
CONFIG = json.loads((ROOT / "config/airtable_sync.json").read_text(encoding="utf-8"))
CONFIG["jurisdiction_field_options_sha256"] = {
    name: canonical_sha256(None) for name in CONFIG["jurisdiction_fields"]
}
CONFIG["rhna_field_options_sha256"] = {
    name: canonical_sha256(None) for name in CONFIG["rhna_fields"]
}
CYCLE_POLICY = {
    "policy_version": 1,
    "default_cycle": "6th",
    "expected_jurisdiction_count": 2,
    "expected_cycle_counts": {"6th": 1, "7th": 1},
    "overrides": [],
    "source": "https://www.hcd.ca.gov/rhna/seventh-cycle",
}


def _source() -> dict[str, object]:
    rows = [
        {
            "jurisdiction": "Example City",
            "jurisdiction_key": "EXAMPLE CITY",
            "cycle": "6th",
            "period_start": "2023-01-01",
            "period_end": "2031-12-31",
            "acutely_low": 0,
            "extremely_low": 0,
            "very_low": 0,
            "low": 2,
            "moderate": 0,
            "above_moderate": 0,
            "total": 2,
            "undated_permits": 0,
            "last_updated": "2026-08-21",
            "data_status": "reported",
        },
        {
            "jurisdiction": "Example County (Unincorporated)",
            "jurisdiction_key": "EXAMPLE COUNTY",
            "cycle": "7th",
            "period_start": "2024-06-30",
            "period_end": "2029-06-30",
            "acutely_low": 0,
            "extremely_low": 0,
            "very_low": 0,
            "low": 0,
            "moderate": 0,
            "above_moderate": 3,
            "total": 3,
            "undated_permits": 0,
            "last_updated": "2026-08-21",
            "data_status": "reported",
        },
    ]
    return {
        "metadata": {
            "cutoff_year": 2025,
            "last_updated": "2026-08-21",
            "provisional": False,
            "complete_after": "2026-06-30",
            "source_manifest_sha256": "b" * 64,
            "dedupe_profile": "test-profile",
            "cycle_policy_sha256": canonical_sha256(CYCLE_POLICY),
            "cycle_counts": {"6th": 1, "7th": 1},
            "jurisdiction_count": 2,
            "selected_row_count": 2,
            "selected_units": 5,
            "retained_row_count": 2,
            "removed_row_count": 0,
            "removed_units": 0,
            "undated_permits": 0,
        },
        "jurisdictions": rows,
    }


def _snapshot() -> dict[str, object]:
    return {
        "snapshot_version": 2,
        "complete": True,
        "retrieved_at": "2026-08-26T12:00:00Z",
        "base_id": CONFIG["base_id"],
        "jurisdictions_table_id": CONFIG["jurisdictions_table_id"],
        "rhna_table_id": CONFIG["rhna_table_id"],
        "schema_sha256": "d" * 64,
        "record_counts": {"jurisdictions": 3, "rhna_records": 2},
        "jurisdictions": [
            {
                "record_id": "recExampleCity0001",
                "name": "Example City",
                "unincorporated": False,
            },
            {
                "record_id": "recExampleCounty01",
                "name": "Example County (Unincorporated Areas)",
                "unincorporated": True,
            },
            {
                "record_id": "recSitekickCity001",
                "name": "Sitekick City",
                "unincorporated": False,
            },
        ],
        "rhna_records": [
            {
                "record_id": "recExampleRhna0001",
                "jurisdiction_record_ids": ["recExampleCity0001"],
                "cycle_record_ids": ["recCycleSixth00001"],
                "cycle": "6th",
                "current": True,
                "correct_link": True,
                "permit_values": {"vli": 0, "li": None, "mi": 0, "ami": 0},
                "rhna_start": "2023-01-01",
                "rhna_end": "2031-12-31",
                "total_progress_override": None,
            },
            {
                "record_id": "recCountyRhna00001",
                "jurisdiction_record_ids": ["recExampleCounty01"],
                "cycle_record_ids": ["recCycleSeventh001"],
                "cycle": "7th",
                "current": True,
                "correct_link": True,
                "permit_values": {"vli": 0, "li": 0, "mi": 0, "ami": 0},
                "rhna_start": "2024-06-30",
                "rhna_end": "2029-06-30",
                "total_progress_override": None,
            },
        ],
    }


def _schema(
    table_id: str, field_map: dict[str, str], types: dict[str, str]
) -> dict[str, object]:
    return {
        "complete": True,
        "base_id": CONFIG["base_id"],
        "table_id": table_id,
        "table_name": "Test",
        "field_count": len(field_map),
        "managed_field_ids": sorted(field_map.values()),
        "fields": [
            {
                "id": field_id,
                "name": logical,
                "type": types[logical],
                "options": None,
            }
            for logical, field_id in field_map.items()
        ],
    }


def _lookup(link: str, value: object) -> dict[str, object]:
    return {
        "linkedRecordIds": [link],
        "valuesByLinkedRecordId": {link: [value]},
    }


def test_project_snapshot_decodes_lookup_wrappers_and_omitted_values() -> None:
    jurisdiction_types = {"name": "singleLineText", "unincorporated": "checkbox"}
    rhna_types = {
        "jurisdiction_link": "multipleRecordLinks",
        "cycle_link": "multipleRecordLinks",
        "cycle": "multipleLookupValues",
        "current": "formula",
        "correct_link": "formula",
        "vli": "number",
        "li": "number",
        "mi": "number",
        "ami": "number",
        "rhna_start": "multipleLookupValues",
        "rhna_end": "multipleLookupValues",
        "total_progress_override": "number",
    }
    jf = CONFIG["jurisdiction_fields"]
    rf = CONFIG["rhna_fields"]
    jurisdiction_export = {
        "complete": True,
        "terminal_offset_reached": True,
        "base_id": CONFIG["base_id"],
        "table_id": CONFIG["jurisdictions_table_id"],
        "record_count": 3,
        "retrieved_at": "2026-08-26T11:00:00Z",
        "records": [
            {"id": "recExampleCity0001", "fields": {jf["name"]: "Example City"}},
            {
                "id": "recExampleCounty01",
                "fields": {
                    jf["name"]: "Example County (Unincorporated Areas)",
                    jf["unincorporated"]: True,
                },
            },
            {"id": "recSitekickCity001", "fields": {jf["name"]: "Sitekick City"}},
        ],
    }
    cycle_link = "recCycleSixth00001"
    rhna_export = {
        "complete": True,
        "terminal_offset_reached": True,
        "base_id": CONFIG["base_id"],
        "table_id": CONFIG["rhna_table_id"],
        "record_count": 1,
        "retrieved_at": "2026-08-26T12:00:00Z",
        "records": [
            {
                "id": "recExampleRhna0001",
                "fields": {
                    rf["jurisdiction_link"]: ["recExampleCity0001"],
                    rf["cycle_link"]: [cycle_link],
                    rf["cycle"]: _lookup(
                        cycle_link,
                        {"id": "selCycleSixth0001", "name": "6th", "color": "green"},
                    ),
                    rf["current"]: 1,
                    rf["correct_link"]: 1,
                    rf["vli"]: 0,
                    rf["mi"]: 0,
                    rf["ami"]: 0,
                    rf["rhna_start"]: _lookup(cycle_link, "2023-01-01"),
                    rf["rhna_end"]: _lookup(cycle_link, "2031-12-31"),
                },
            }
        ],
    }
    local_config = copy.deepcopy(CONFIG)
    local_config["expected_jurisdiction_record_count"] = 3
    snapshot = project_airtable_snapshot(
        config=local_config,
        jurisdiction_schema=_schema(
            CONFIG["jurisdictions_table_id"], jf, jurisdiction_types
        ),
        rhna_schema=_schema(CONFIG["rhna_table_id"], rf, rhna_types),
        jurisdiction_export=jurisdiction_export,
        rhna_export=rhna_export,
    )
    row = snapshot["rhna_records"][0]
    assert row["cycle"] == "6th"
    assert row["current"] is True
    assert row["permit_values"]["li"] is None
    assert row["total_progress_override"] is None
    assert snapshot["retrieved_at"] == "2026-08-26T12:00:00Z"

    rest_export = copy.deepcopy(rhna_export)
    rest_fields = rest_export["records"][0]["fields"]
    rest_fields[rf["cycle"]] = ["6th"]
    rest_fields[rf["rhna_start"]] = ["2023-01-01"]
    rest_fields[rf["rhna_end"]] = ["2031-12-31"]
    rest_snapshot = project_airtable_snapshot(
        config=local_config,
        jurisdiction_schema=_schema(
            CONFIG["jurisdictions_table_id"], jf, jurisdiction_types
        ),
        rhna_schema=_schema(CONFIG["rhna_table_id"], rf, rhna_types),
        jurisdiction_export=jurisdiction_export,
        rhna_export=rest_export,
    )
    rest_row = rest_snapshot["rhna_records"][0]
    assert rest_row["cycle"] == "6th"
    assert rest_row["rhna_start"] == "2023-01-01"
    assert rest_row["rhna_end"] == "2031-12-31"

    rest_fields[rf["cycle_link"]] = ["recCycleFifth00001"]
    rest_fields[rf["cycle"]] = ["5th"]
    historical_snapshot = project_airtable_snapshot(
        config=local_config,
        jurisdiction_schema=_schema(
            CONFIG["jurisdictions_table_id"], jf, jurisdiction_types
        ),
        rhna_schema=_schema(CONFIG["rhna_table_id"], rf, rhna_types),
        jurisdiction_export=jurisdiction_export,
        rhna_export=rest_export,
    )
    assert historical_snapshot["rhna_records"][0]["cycle"] == "5th"


def test_project_snapshot_rejects_schema_type_drift() -> None:
    jf = CONFIG["jurisdiction_fields"]
    bad_schema = _schema(
        CONFIG["jurisdictions_table_id"],
        jf,
        {"name": "singleLineText", "unincorporated": "formula"},
    )
    with pytest.raises(AirtableSyncSnapshotError, match="pinned type"):
        project_airtable_snapshot(
            config={**CONFIG, "expected_jurisdiction_record_count": 3},
            jurisdiction_schema=bad_schema,
            rhna_schema={},
            jurisdiction_export={},
            rhna_export={},
        )


def test_plan_is_cycle_aware_digest_bound_and_converts_to_updates() -> None:
    local_config = copy.deepcopy(CONFIG)
    local_config["expected_jurisdiction_record_count"] = 3
    plan = build_airtable_sync_plan(
        totals=_source(),
        snapshot=_snapshot(),
        config=local_config,
        cycle_policy=CYCLE_POLICY,
        git_sha="a" * 40,
    )
    assert plan["apply_eligible"] is True
    assert plan["matched_count"] == 2
    assert plan["change_count"] == 2
    assert plan["source_aggregate_after"]["total"] == 5
    assert plan["cycle_counts"] == {"6th": 1, "7th": 1}
    updates = plan_record_updates(plan)
    assert len(updates) == 2
    city_update = next(
        update for update in updates if update.record_id == "recExampleRhna0001"
    )
    li_field = CONFIG["rhna_fields"]["li"]
    assert city_update.expected_fields == {li_field: None}
    assert city_update.desired_fields == {li_field: 2}
    summary = summarize_airtable_sync_plan(plan)
    rendered = json.dumps(summary)
    assert "recExample" not in rendered
    assert summary["source_total"] == 5

    same_state = copy.deepcopy(_snapshot())
    same_state["retrieved_at"] = "2026-08-26T13:00:00Z"
    verify_airtable_sync_plan(plan, same_state)
    changed_state = copy.deepcopy(same_state)
    changed_state["rhna_records"][0]["permit_values"]["li"] = 1
    with pytest.raises(AirtableSyncStateChangedError):
        verify_airtable_sync_plan(plan, changed_state)


def test_not_current_target_blocks_apply() -> None:
    local_config = copy.deepcopy(CONFIG)
    local_config["expected_jurisdiction_record_count"] = 3
    snapshot = _snapshot()
    snapshot["rhna_records"][0]["current"] = False
    plan = build_airtable_sync_plan(
        totals=_source(),
        snapshot=snapshot,
        config=local_config,
        cycle_policy=CYCLE_POLICY,
        git_sha="a" * 40,
    )
    assert plan["apply_eligible"] is False
    assert summarize_airtable_sync_plan(plan)["blocker_codes"] == [
        "airtable_target_not_current"
    ]
    with pytest.raises(AirtableSyncBlockedError):
        plan_record_updates(plan)


def test_source_digest_is_exact() -> None:
    local_config = copy.deepcopy(CONFIG)
    local_config["expected_jurisdiction_record_count"] = 3
    source = _source()
    plan = build_airtable_sync_plan(
        totals=source,
        snapshot=_snapshot(),
        config=local_config,
        cycle_policy=CYCLE_POLICY,
        git_sha="a" * 40,
    )
    assert plan["source_sha256"] == canonical_sha256(source)


def test_blank_permit_cell_is_planned_as_an_explicit_zero() -> None:
    local_config = copy.deepcopy(CONFIG)
    local_config["expected_jurisdiction_record_count"] = 3
    snapshot = _snapshot()
    snapshot["rhna_records"][0]["permit_values"]["mi"] = None

    plan = build_airtable_sync_plan(
        totals=_source(),
        snapshot=snapshot,
        config=local_config,
        cycle_policy=CYCLE_POLICY,
        git_sha="a" * 40,
    )

    change = next(
        item
        for item in plan["changes"]
        if item["airtable_record_id"] == "recExampleRhna0001"
    )
    mi_field = CONFIG["rhna_fields"]["mi"]
    assert change["expected_fields"][mi_field] is None
    assert change["desired_fields"][mi_field] == 0


def test_current_cycle_policy_digest_is_required() -> None:
    local_config = copy.deepcopy(CONFIG)
    local_config["expected_jurisdiction_record_count"] = 3

    with pytest.raises(
        AirtableSyncConfigurationError,
        match="not built from the current cycle policy",
    ):
        build_airtable_sync_plan(
            totals=_source(),
            snapshot=_snapshot(),
            config=local_config,
            cycle_policy={**CYCLE_POLICY, "policy_version": 2},
            git_sha="a" * 40,
        )


@pytest.mark.parametrize("logical_name", ["current", "cycle"])
def test_project_snapshot_rejects_formula_and_lookup_option_drift(
    logical_name: str,
) -> None:
    jf = CONFIG["jurisdiction_fields"]
    rf = CONFIG["rhna_fields"]
    rhna_types = {
        "jurisdiction_link": "multipleRecordLinks",
        "cycle_link": "multipleRecordLinks",
        "cycle": "multipleLookupValues",
        "current": "formula",
        "correct_link": "formula",
        "vli": "number",
        "li": "number",
        "mi": "number",
        "ami": "number",
        "rhna_start": "multipleLookupValues",
        "rhna_end": "multipleLookupValues",
        "total_progress_override": "number",
    }
    rhna_schema = _schema(CONFIG["rhna_table_id"], rf, rhna_types)
    field = next(
        item for item in rhna_schema["fields"] if item["id"] == rf[logical_name]
    )
    field["options"] = {"unexpected": True}

    with pytest.raises(AirtableSyncSnapshotError, match="pinned options"):
        project_airtable_snapshot(
            config={**CONFIG, "expected_jurisdiction_record_count": 3},
            jurisdiction_schema=_schema(
                CONFIG["jurisdictions_table_id"],
                jf,
                {"name": "singleLineText", "unincorporated": "checkbox"},
            ),
            rhna_schema=rhna_schema,
            jurisdiction_export={},
            rhna_export={},
        )
