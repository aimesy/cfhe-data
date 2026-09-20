# visibility: public
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from cfhe_data.airtable_sync import (
    APR_DATE_FIELDS,
    JURISDICTION_FIELD_TYPES,
    RHNA_FIELD_TYPES,
    AirtableSyncBlockedError,
    AirtableSyncConfigurationError,
    AirtableSyncError,
    AirtableSyncSnapshotError,
    AirtableSyncStateChangedError,
    build_airtable_sync_plan,
    fetch_airtable_snapshot,
    normalize_sync_config,
    plan_record_updates,
    plan_verification_updates,
    project_airtable_snapshot,
    summarize_airtable_sync_plan,
    verify_airtable_sync_plan,
)
from cfhe_data.webflow import canonical_sha256

ROOT = Path(__file__).resolve().parents[1]
CONFIG = json.loads((ROOT / "config/airtable_sync.json").read_text(encoding="utf-8"))
# Keep version-one fixtures independent of the production configuration upgrade.
CONFIG["config_version"] = 1
for name in APR_DATE_FIELDS:
    CONFIG["rhna_fields"].pop(name, None)
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
        "last_verified": "dateTime",
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
    reconciled = copy.deepcopy(plan)
    reconciled["change_count"] = 0
    verification_updates = plan_verification_updates(reconciled, "2026-08-28T22:30:00Z")
    assert len(verification_updates) == 2
    verified_field = CONFIG["rhna_fields"]["last_verified"]
    assert verification_updates[0].expected_fields == {verified_field: None}
    assert verification_updates[0].desired_fields == {
        verified_field: "2026-08-28T22:30:00Z"
    }
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
        "last_verified": "dateTime",
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


def _date_config() -> dict[str, object]:
    config = copy.deepcopy(CONFIG)
    config["config_version"] = 2
    config["expected_jurisdiction_record_count"] = 3
    for index, name in enumerate(APR_DATE_FIELDS):
        config["rhna_fields"][name] = f"fldAprPaceDate{index}"
        config["rhna_field_options_sha256"][name] = canonical_sha256(None)
    return config


def _date_snapshot(*, matching_permits: bool = False) -> dict[str, object]:
    snapshot = _snapshot()
    for row in snapshot["rhna_records"]:
        row["pace_dates"] = dict.fromkeys(APR_DATE_FIELDS)
    if matching_permits:
        snapshot["rhna_records"][0]["permit_values"]["li"] = 2
        snapshot["rhna_records"][1]["permit_values"]["ami"] = 3
    return snapshot


def _raw_date_snapshot_inputs(config: dict[str, object]) -> dict[str, object]:
    snapshot = _date_snapshot()
    jf = config["jurisdiction_fields"]
    rf = config["rhna_fields"]
    jurisdiction_records = [
        {
            "id": row["record_id"],
            "fields": {
                jf["name"]: row["name"],
                jf["unincorporated"]: row["unincorporated"],
            },
        }
        for row in snapshot["jurisdictions"]
    ]
    rhna_records = []
    for row in snapshot["rhna_records"]:
        values = {
            "jurisdiction_link": row["jurisdiction_record_ids"],
            "cycle_link": row["cycle_record_ids"],
            "cycle": [row["cycle"]],
            "current": 1,
            "correct_link": 1,
            "rhna_start": [row["rhna_start"]],
            "rhna_end": [row["rhna_end"]],
            **row["permit_values"],
        }
        rhna_records.append(
            {
                "id": row["record_id"],
                "fields": {rf[name]: value for name, value in values.items()},
            }
        )
    exports = {}
    for name, records in (
        ("jurisdiction", jurisdiction_records),
        ("rhna", rhna_records),
    ):
        table_id = config[
            "jurisdictions_table_id" if name == "jurisdiction" else "rhna_table_id"
        ]
        exports[f"{name}_export"] = {
            "complete": True,
            "terminal_offset_reached": True,
            "base_id": config["base_id"],
            "table_id": table_id,
            "record_count": len(records),
            "retrieved_at": snapshot["retrieved_at"],
            "records": records,
        }
    return {
        "config": config,
        "jurisdiction_schema": _schema(
            config["jurisdictions_table_id"], jf, JURISDICTION_FIELD_TYPES
        ),
        "rhna_schema": _schema(config["rhna_table_id"], rf, RHNA_FIELD_TYPES),
        **exports,
    }


def test_version_two_requires_all_date_fields_and_option_pins() -> None:
    assert normalize_sync_config(CONFIG)["config_version"] == 1
    config = _date_config()
    assert normalize_sync_config(config)["config_version"] == 2
    for mapping in ("rhna_fields", "rhna_field_options_sha256"):
        incomplete = copy.deepcopy(config)
        incomplete[mapping].pop("apr_data_through")
        with pytest.raises(
            AirtableSyncConfigurationError, match="missing apr_data_through"
        ):
            normalize_sync_config(incomplete)


@pytest.mark.parametrize("version", [True, 1.0, 3])
def test_config_version_must_be_supported_integer(version: object) -> None:
    with pytest.raises(AirtableSyncConfigurationError, match="config_version"):
        normalize_sync_config({**CONFIG, "config_version": version})


def test_date_plan_uses_reporting_cutoff_and_actual_planning_periods() -> None:
    config = _date_config()
    plan = build_airtable_sync_plan(
        totals=_source(),
        snapshot=_date_snapshot(),
        config=config,
        cycle_policy=CYCLE_POLICY,
        git_sha="a" * 40,
    )
    updates = plan_record_updates(plan)
    rf = config["rhna_fields"]
    assert plan["source_aggregate_after"]["total"] == 5
    assert plan["source_last_updated"] == "2026-08-21"
    for update in updates:
        assert update.desired_fields[rf["apr_data_through"]] == "2025-12-31"
        assert update.expected_fields[rf["apr_data_through"]] is None
    county = next(
        update for update in updates if update.record_id == "recCountyRhna00001"
    )
    assert county.desired_fields[rf["apr_planning_start"]] == "2024-06-30"
    assert county.desired_fields[rf["apr_planning_end"]] == "2029-06-30"
    assert county.desired_fields[rf["ami"]] == 3


def test_date_only_changes_reconcile_without_changing_permit_totals() -> None:
    config = _date_config()
    snapshot = _date_snapshot(matching_permits=True)
    source = _source()
    for row, source_row in zip(snapshot["rhna_records"], source["jurisdictions"]):
        row["pace_dates"] = {
            "apr_data_through": "2024-12-31",
            "apr_planning_start": source_row["period_start"],
            "apr_planning_end": source_row["period_end"],
        }
    plan = build_airtable_sync_plan(
        totals=source,
        snapshot=snapshot,
        config=config,
        cycle_policy=CYCLE_POLICY,
        git_sha="a" * 40,
    )
    assert plan["change_count"] == 2
    assert plan["airtable_aggregate_before"] == plan["source_aggregate_after"]
    field = config["rhna_fields"]["apr_data_through"]
    for update in plan_record_updates(plan):
        assert update.expected_fields == {field: "2024-12-31"}
        assert update.desired_fields == {field: "2025-12-31"}
    with pytest.raises(AirtableSyncBlockedError, match="fully reconciled"):
        plan_verification_updates(plan, "2026-08-28T22:30:00Z")
    changed_state = copy.deepcopy(snapshot)
    changed_state["rhna_records"][0]["pace_dates"]["apr_data_through"] = "2023-12-31"
    with pytest.raises(AirtableSyncStateChangedError):
        verify_airtable_sync_plan(plan, changed_state)
    for row in snapshot["rhna_records"]:
        row["pace_dates"]["apr_data_through"] = "2025-12-31"
    reconciled = build_airtable_sync_plan(
        totals=source,
        snapshot=snapshot,
        config=config,
        cycle_policy=CYCLE_POLICY,
        git_sha="a" * 40,
    )
    assert reconciled["change_count"] == 0
    assert reconciled["unchanged_count"] == 2
    assert plan_record_updates(reconciled) == []
    assert len(plan_verification_updates(reconciled, "2026-08-28T22:30:00Z")) == 2


def test_version_two_rejects_a_snapshot_without_managed_dates() -> None:
    with pytest.raises(AirtableSyncSnapshotError, match="pace dates are missing"):
        build_airtable_sync_plan(
            totals=_source(),
            snapshot=_snapshot(),
            config=_date_config(),
            cycle_policy=CYCLE_POLICY,
            git_sha="a" * 40,
        )


@pytest.mark.parametrize(
    "value", ["2025-02-29", "2025-13-01", "20251231", "2025-12-31T00:00:00Z"]
)
def test_invalid_snapshot_date_is_rejected(value: str) -> None:
    config = _date_config()
    inputs = _raw_date_snapshot_inputs(config)
    inputs["rhna_export"]["records"][0]["fields"][
        config["rhna_fields"]["apr_data_through"]
    ] = value
    with pytest.raises(
        AirtableSyncSnapshotError, match="apr_data_through must be an ISO date"
    ):
        project_airtable_snapshot(**inputs)


def test_project_snapshot_preserves_dates_and_empty_old_values() -> None:
    config = _date_config()
    inputs = _raw_date_snapshot_inputs(config)
    fields = inputs["rhna_export"]["records"][0]["fields"]
    fields[config["rhna_fields"]["apr_data_through"]] = "2024-12-31"
    fields[config["rhna_fields"]["apr_planning_start"]] = ""
    snapshot = project_airtable_snapshot(**inputs)
    row = next(
        row
        for row in snapshot["rhna_records"]
        if row["record_id"] == "recExampleRhna0001"
    )
    assert row["pace_dates"] == {
        "apr_data_through": "2024-12-31",
        "apr_planning_start": "",
        "apr_planning_end": None,
    }


@pytest.mark.parametrize(
    "attribute, value, message",
    [
        ("type", "dateTime", "pinned type"),
        ("options", {"dateFormat": {"name": "us"}}, "pinned options"),
    ],
)
def test_date_schema_drift_is_rejected(
    attribute: str, value: object, message: str
) -> None:
    config = _date_config()
    inputs = _raw_date_snapshot_inputs(config)
    field_id = config["rhna_fields"]["apr_data_through"]
    field = next(
        item for item in inputs["rhna_schema"]["fields"] if item["id"] == field_id
    )
    field[attribute] = value
    with pytest.raises(AirtableSyncSnapshotError, match=message):
        project_airtable_snapshot(**inputs)


@pytest.mark.parametrize(
    "field, value",
    [
        ("period_start", "2023-02-29"),
        ("period_end", "2031-13-01"),
        ("period_end", "2023-01-01"),
        ("period_end", "2022-12-31"),
    ],
)
def test_source_planning_dates_must_form_a_valid_positive_period(
    field: str, value: str
) -> None:
    source = _source()
    source["jurisdictions"][0][field] = value
    with pytest.raises(AirtableSyncError, match="invalid|positive duration"):
        build_airtable_sync_plan(
            totals=source,
            snapshot=_date_snapshot(),
            config=_date_config(),
            cycle_policy=CYCLE_POLICY,
            git_sha="a" * 40,
        )


def test_version_two_client_write_allowlist_adds_only_the_apr_dates(
    monkeypatch,
) -> None:
    config = _date_config()
    inputs = _raw_date_snapshot_inputs(config)
    clients = []

    class FakeClient:
        def __init__(self, **kwargs):
            self.config = kwargs
            clients.append(self)

        def get_table_schema(self):
            prefix = (
                "jurisdiction"
                if self.config["table_id"] == config["jurisdictions_table_id"]
                else "rhna"
            )
            return inputs[f"{prefix}_schema"]

        def list_records(self, **kwargs):
            prefix = (
                "jurisdiction"
                if self.config["table_id"] == config["jurisdictions_table_id"]
                else "rhna"
            )
            return inputs[f"{prefix}_export"]

    monkeypatch.setattr("cfhe_data.airtable_sync.AirtableClient", FakeClient)
    snapshot, client = fetch_airtable_snapshot(token="test-token", config=config)
    assert snapshot["complete"] is True
    assert client is clients[1]
    rf = config["rhna_fields"]
    assert set(client.config["writable_field_ids"]) == {
        rf[name]
        for name in ("vli", "li", "mi", "ami", "last_verified", *APR_DATE_FIELDS)
    }
