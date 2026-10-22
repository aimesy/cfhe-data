# visibility: public
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from cfhe_data.airtable_sync import (
    APR_DATE_FIELDS,
    ENTITLEMENT_BANDS,
    ENTITLEMENT_CATEGORY_FIELDS,
    ENTITLEMENT_METADATA_FIELDS,
    JURISDICTION_FIELD_TYPES,
    RHNA_FIELD_TYPES,
    AirtableSyncBlockedError,
    AirtableSyncConfigurationError,
    AirtableSyncError,
    AirtableSyncSnapshotError,
    AirtableSyncStateChangedError,
    _validate_source,
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
for name in (*APR_DATE_FIELDS, *ENTITLEMENT_BANDS):
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


@pytest.mark.parametrize("version", [True, 0, 1.0, 3.0, 4])
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


# Digests from the code at 2d26552, before config version 3 existed. Version 3
# must leave version 1 and 2 configs, snapshots, plans and summaries unchanged.
PINNED_DIGESTS = {
    "v1_config": "176ebd86778f67e29b589a15cbca12e559d084be7a8f56086276c5e2817ee48c",
    "v1_snapshot": "e639f598856e412cdb0498bee52005c24ab89b3177df7921974927907b1bc82c",
    "v1_plan": "1a600a5cf28a1b1bc0e9b0414b8cab1a4aabf429e7548462b58cc63ab5ff7a69",
    "v1_summary": "f29693278381d900a0c320ffaa8b2f1ebf8942aa0c77e64fdd6ece615ffa93a9",
    "v1_blocked_plan": (
        "3853a914fa45936a7b6ccdd120bcb5ced76eaae51bb78c6ac37c069a38fde1a8"
    ),
    "v2_config": "f4f66790882c8d941644ee33a674afe233babbd30ac51871e51ae6fba8221a5e",
    "v2_snapshot": "11f9962a5fa8b090ab7f2513b51491eca529638335b3df24786f8d5e556f1889",
    "v2_plan": "c3279c7d42043532ba7a0466581ceed6132dcd97b0b8e10ec1dab5b0aa8a5f23",
    "v2_summary": "cfd8bae23dabb9db039305492afcc95e88f14822702dcaefa6f82f79ec598d9f",
}


def test_versions_one_and_two_keep_their_pinned_digests() -> None:
    v1_config = copy.deepcopy(CONFIG)
    v1_config["expected_jurisdiction_record_count"] = 3
    v1_plan = build_airtable_sync_plan(
        totals=_source(),
        snapshot=_snapshot(),
        config=v1_config,
        cycle_policy=CYCLE_POLICY,
        git_sha="a" * 40,
    )
    blocked_snapshot = _snapshot()
    blocked_snapshot["rhna_records"][0]["current"] = False
    blocked_snapshot["rhna_records"][1]["total_progress_override"] = 4
    v1_blocked_plan = build_airtable_sync_plan(
        totals=_source(),
        snapshot=blocked_snapshot,
        config=v1_config,
        cycle_policy=CYCLE_POLICY,
        git_sha="a" * 40,
    )
    v2_plan = build_airtable_sync_plan(
        totals=_source(),
        snapshot=_date_snapshot(),
        config=_date_config(),
        cycle_policy=CYCLE_POLICY,
        git_sha="a" * 40,
    )
    observed = {
        "v1_config": canonical_sha256(normalize_sync_config(v1_config)),
        "v1_snapshot": canonical_sha256(
            project_airtable_snapshot(**_raw_date_snapshot_inputs(v1_config))
        ),
        "v1_plan": v1_plan["plan_sha256"],
        "v1_summary": canonical_sha256(summarize_airtable_sync_plan(v1_plan)),
        "v1_blocked_plan": v1_blocked_plan["plan_sha256"],
        "v2_config": canonical_sha256(normalize_sync_config(_date_config())),
        "v2_snapshot": canonical_sha256(
            project_airtable_snapshot(**_raw_date_snapshot_inputs(_date_config()))
        ),
        "v2_plan": v2_plan["plan_sha256"],
        "v2_summary": canonical_sha256(summarize_airtable_sync_plan(v2_plan)),
    }
    assert observed == PINNED_DIGESTS
    for plan in (v1_plan, v2_plan):
        assert "source_entitlement_aggregate_after" not in plan
        assert "source_entitlement_total" not in summarize_airtable_sync_plan(plan)


def _entitlement_config() -> dict[str, object]:
    config = _date_config()
    config["config_version"] = 3
    for index, name in enumerate(ENTITLEMENT_BANDS):
        config["rhna_fields"][name] = f"fldEntitlement{index}"
        config["rhna_field_options_sha256"][name] = canonical_sha256(None)
    return config


def _add_entitlements(totals: dict[str, object]) -> dict[str, object]:
    first, *rest = totals["jurisdictions"]
    first.update(
        entitlement_acutely_low=0,
        entitlement_extremely_low=1,
        entitlement_very_low=2,
        entitlement_low=3,
        entitlement_moderate=0,
        entitlement_above_moderate=4,
        entitlement_total=10,
        undated_entitlements=1,
        entitlement_data_status="reported",
    )
    for row in rest:
        row.update(
            dict.fromkeys(ENTITLEMENT_CATEGORY_FIELDS, 0),
            entitlement_total=0,
            undated_entitlements=0,
            entitlement_data_status="no_selected_rows",
        )
    totals["metadata"].update(
        entitlement_selected_row_count=4,
        entitlement_selected_units=12,
        entitlement_retained_row_count=3,
        entitlement_removed_row_count=1,
        entitlement_removed_units=2,
        undated_entitlements=1,
    )
    return totals


def _entitlement_snapshot(*, reconciled: bool = False) -> dict[str, object]:
    snapshot = _date_snapshot(matching_permits=reconciled)
    for row in snapshot["rhna_records"]:
        row["entitlement_values"] = dict.fromkeys(ENTITLEMENT_BANDS)
    if reconciled:
        for row, source_row in zip(
            snapshot["rhna_records"], _source()["jurisdictions"], strict=True
        ):
            row["pace_dates"] = {
                "apr_data_through": "2025-12-31",
                "apr_planning_start": source_row["period_start"],
                "apr_planning_end": source_row["period_end"],
            }
    return snapshot


def _entitlement_plan(
    snapshot: dict[str, object], totals: dict[str, object] | None = None
) -> dict[str, object]:
    return build_airtable_sync_plan(
        totals=_add_entitlements(_source()) if totals is None else totals,
        snapshot=snapshot,
        config=_entitlement_config(),
        cycle_policy=CYCLE_POLICY,
        git_sha="a" * 40,
    )


def test_version_three_requires_all_entitlement_fields_and_option_pins() -> None:
    config = _entitlement_config()
    assert normalize_sync_config(config)["config_version"] == 3
    for mapping in ("rhna_fields", "rhna_field_options_sha256"):
        for name in (ENTITLEMENT_BANDS[0], APR_DATE_FIELDS[0]):
            incomplete = copy.deepcopy(config)
            incomplete[mapping].pop(name)
            with pytest.raises(AirtableSyncConfigurationError, match=f"missing {name}"):
                normalize_sync_config(incomplete)
    earlier = copy.deepcopy(config)
    earlier["config_version"] = 2
    with pytest.raises(AirtableSyncConfigurationError):
        normalize_sync_config(earlier)


@pytest.mark.parametrize(
    "section, field, value, message",
    [
        ("row", "entitlement_low", True, "nonnegative integer"),
        ("row", "entitlement_low", None, "nonnegative integer"),
        ("row", "entitlement_low", -1, "nonnegative integer"),
        ("row", "entitlement_low", 3.0, "nonnegative integer"),
        ("row", "entitlement_total", False, "nonnegative integer"),
        ("row", "entitlement_total", 11, "six entitlement categories"),
        ("row", "undated_entitlements", 11, "cannot exceed entitlement_total"),
        ("row", "entitlement_data_status", "unknown", "status is invalid"),
        ("row", "entitlement_data_status", "no_selected_rows", "has entitlements"),
        ("metadata", "entitlement_selected_units", None, "nonnegative integer"),
        ("metadata", "entitlement_removed_units", True, "nonnegative integer"),
        ("metadata", "entitlement_removed_row_count", -1, "nonnegative integer"),
        ("metadata", "entitlement_retained_row_count", 4, "row counts"),
        ("metadata", "entitlement_removed_units", 13, "exceeds"),
        ("metadata", "entitlement_selected_units", 13, "totals do not reconcile"),
        ("metadata", "undated_entitlements", 0, "undated entitlements"),
    ],
)
def test_version_three_rejects_malformed_entitlement_source(
    section: str, field: str, value: object, message: str
) -> None:
    totals = _add_entitlements(_source())
    target = totals["jurisdictions"][0] if section == "row" else totals["metadata"]
    target[field] = value
    with pytest.raises(AirtableSyncError, match=message):
        _entitlement_plan(_entitlement_snapshot(), totals)


@pytest.mark.parametrize(
    "change, message",
    [
        ("artifact", "lack the entitlement fields"),
        ("row", "row 1 fields changed"),
        ("metadata", "metadata fields changed"),
    ],
)
def test_version_three_requires_every_entitlement_field(
    change: str, message: str
) -> None:
    totals = _add_entitlements(_source())
    if change == "artifact":
        totals = _source()
    elif change == "row":
        del totals["jurisdictions"][1]["entitlement_data_status"]
    else:
        del totals["metadata"]["entitlement_removed_units"]
    with pytest.raises(AirtableSyncError, match=message):
        _entitlement_plan(_entitlement_snapshot(), totals)


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("section", ["metadata", "rows"])
def test_earlier_versions_still_reject_entitlement_fields(
    version: int, section: str
) -> None:
    if version == 1:
        config = copy.deepcopy(CONFIG)
        config["expected_jurisdiction_record_count"] = 3
        snapshot = _snapshot()
    else:
        config, snapshot = _date_config(), _date_snapshot()
    totals = _add_entitlements(_source())
    message = "metadata fields changed"
    if section == "rows":
        for field in ENTITLEMENT_METADATA_FIELDS:
            del totals["metadata"][field]
        message = "row 0 fields changed"
    with pytest.raises(AirtableSyncError, match=message):
        build_airtable_sync_plan(
            totals=totals,
            snapshot=snapshot,
            config=config,
            cycle_policy=CYCLE_POLICY,
            git_sha="a" * 40,
        )
    with pytest.raises(AirtableSyncError, match=message):
        _validate_source(totals)


def test_version_three_writes_integer_bands_and_zero_for_no_selected_rows() -> None:
    plan = _entitlement_plan(_entitlement_snapshot())
    rf = _entitlement_config()["rhna_fields"]
    assert plan["apply_eligible"] is True
    assert plan["intent"] == (
        "update_existing_permit_entitlement_and_apr_date_fields_only"
    )
    assert plan["target"]["entitlement_field_ids"] == {
        band: rf[band] for band in ENTITLEMENT_BANDS
    }
    updates = {update.record_id: update for update in plan_record_updates(plan)}
    city = updates["recExampleRhna0001"]
    county = updates["recCountyRhna00001"]
    assert {band: city.desired_fields[rf[band]] for band in ENTITLEMENT_BANDS} == {
        "entitlement_vli": 3,
        "entitlement_li": 3,
        "entitlement_mi": 0,
        "entitlement_ami": 4,
    }
    for band in ENTITLEMENT_BANDS:
        assert county.expected_fields[rf[band]] is None
        assert county.desired_fields[rf[band]] == 0
        for update in (city, county):
            assert type(update.desired_fields[rf[band]]) is int
    assert city.desired_fields[rf["li"]] == 2
    assert county.desired_fields[rf["ami"]] == 3
    change = next(
        item
        for item in plan["changes"]
        if item["airtable_record_id"] == "recCountyRhna00001"
    )
    assert change["entitlement_data_status"] == "no_selected_rows"
    assert change["after_entitlement_total"] == 0
    assert change["after_total"] == 3
    # Entitlements never enter the permit aggregates or the permit summary total.
    assert plan["source_aggregate_after"] == {
        "vli": 0,
        "li": 2,
        "mi": 0,
        "ami": 3,
        "total": 5,
    }
    assert plan["source_entitlement_aggregate_after"] == {
        "entitlement_vli": 3,
        "entitlement_li": 3,
        "entitlement_mi": 0,
        "entitlement_ami": 4,
        "total": 10,
    }
    assert plan["airtable_entitlement_aggregate_before"]["total"] == 0
    summary = summarize_airtable_sync_plan(plan)
    assert summary["source_total"] == 5
    assert summary["source_entitlement_total"] == 10
    assert "recExample" not in json.dumps(summary)


def test_entitlement_only_mismatch_blocks_verification_until_reconciled() -> None:
    snapshot = _entitlement_snapshot(reconciled=True)
    plan = _entitlement_plan(snapshot)
    rf = _entitlement_config()["rhna_fields"]
    entitlement_ids = {rf[band] for band in ENTITLEMENT_BANDS}
    assert plan["change_count"] == 2
    assert plan["airtable_aggregate_before"] == plan["source_aggregate_after"]
    for update in plan_record_updates(plan):
        assert set(update.desired_fields) == entitlement_ids
    with pytest.raises(AirtableSyncBlockedError, match="fully reconciled"):
        plan_verification_updates(plan, "2026-08-28T22:30:00Z")
    changed_state = copy.deepcopy(snapshot)
    changed_state["rhna_records"][0]["entitlement_values"]["entitlement_li"] = 3
    with pytest.raises(AirtableSyncStateChangedError):
        verify_airtable_sync_plan(plan, changed_state)

    for row, change in zip(snapshot["rhna_records"], plan["changes"], strict=True):
        assert row["record_id"] == change["airtable_record_id"]
        row["entitlement_values"] = {
            band: change["desired_fields"][rf[band]] for band in ENTITLEMENT_BANDS
        }
    reconciled = _entitlement_plan(snapshot)
    assert reconciled["change_count"] == 0
    assert reconciled["unchanged_count"] == 2
    assert (
        reconciled["airtable_entitlement_aggregate_before"]
        == reconciled["source_entitlement_aggregate_after"]
    )
    assert len(plan_verification_updates(reconciled, "2026-08-28T22:30:00Z")) == 2


def test_progress_overrides_block_every_version_three_write() -> None:
    snapshot = _entitlement_snapshot(reconciled=True)
    snapshot["rhna_records"][1]["total_progress_override"] = 4
    plan = _entitlement_plan(snapshot)
    assert plan["change_count"] == 2
    assert plan["apply_eligible"] is False
    assert summarize_airtable_sync_plan(plan)["blocker_codes"] == [
        "airtable_progress_overrides_present"
    ]
    with pytest.raises(AirtableSyncBlockedError):
        plan_record_updates(plan)

    for row in snapshot["rhna_records"]:
        row["entitlement_values"] = dict.fromkeys(ENTITLEMENT_BANDS, 0)
    snapshot["rhna_records"][0]["entitlement_values"].update(
        entitlement_vli=3, entitlement_li=3, entitlement_ami=4
    )
    reconciled = _entitlement_plan(snapshot)
    assert reconciled["change_count"] == 0
    assert reconciled["apply_eligible"] is False
    with pytest.raises(AirtableSyncBlockedError):
        plan_verification_updates(reconciled, "2026-08-28T22:30:00Z")


@pytest.mark.parametrize("value", [None, {}, {"entitlement_vli": 0}])
def test_version_three_rejects_a_snapshot_without_entitlement_values(
    value: object,
) -> None:
    snapshot = _entitlement_snapshot()
    snapshot["rhna_records"][0]["entitlement_values"] = value
    with pytest.raises(AirtableSyncSnapshotError, match="entitlement values"):
        _entitlement_plan(snapshot)


def test_project_snapshot_reads_entitlement_cells() -> None:
    config = _entitlement_config()
    inputs = _raw_date_snapshot_inputs(config)
    fields = inputs["rhna_export"]["records"][0]["fields"]
    fields[config["rhna_fields"]["entitlement_vli"]] = 3
    fields[config["rhna_fields"]["entitlement_li"]] = 0
    snapshot = project_airtable_snapshot(**inputs)
    row = next(
        row
        for row in snapshot["rhna_records"]
        if row["record_id"] == "recExampleRhna0001"
    )
    assert row["entitlement_values"] == {
        "entitlement_vli": 3,
        "entitlement_li": 0,
        "entitlement_mi": None,
        "entitlement_ami": None,
    }
    assert "pace_dates" in row
    earlier = project_airtable_snapshot(**_raw_date_snapshot_inputs(_date_config()))
    assert all("entitlement_values" not in row for row in earlier["rhna_records"])


@pytest.mark.parametrize("value", [True, -1, 1.5, "3"])
def test_invalid_snapshot_entitlement_is_rejected(value: object) -> None:
    config = _entitlement_config()
    inputs = _raw_date_snapshot_inputs(config)
    inputs["rhna_export"]["records"][0]["fields"][
        config["rhna_fields"]["entitlement_vli"]
    ] = value
    with pytest.raises(AirtableSyncSnapshotError, match="entitlement_vli must be"):
        project_airtable_snapshot(**inputs)


@pytest.mark.parametrize(
    "attribute, value, message",
    [
        ("type", "formula", "pinned type"),
        ("options", {"precision": 2}, "pinned options"),
    ],
)
def test_entitlement_schema_drift_is_rejected(
    attribute: str, value: object, message: str
) -> None:
    config = _entitlement_config()
    inputs = _raw_date_snapshot_inputs(config)
    field_id = config["rhna_fields"]["entitlement_ami"]
    field = next(
        item for item in inputs["rhna_schema"]["fields"] if item["id"] == field_id
    )
    field[attribute] = value
    with pytest.raises(AirtableSyncSnapshotError, match=message):
        project_airtable_snapshot(**inputs)


def test_version_three_client_allowlists_add_only_the_entitlement_fields(
    monkeypatch,
) -> None:
    clients = []

    def fetch(config: dict[str, object]) -> dict[str, object]:
        inputs = _raw_date_snapshot_inputs(config)

        class FakeClient:
            def __init__(self, **kwargs):
                self.config = kwargs
                clients.append(self)

            def _prefix(self):
                return (
                    "jurisdiction"
                    if self.config["table_id"] == config["jurisdictions_table_id"]
                    else "rhna"
                )

            def get_table_schema(self):
                return inputs[f"{self._prefix()}_schema"]

            def list_records(self, **kwargs):
                return inputs[f"{self._prefix()}_export"]

        monkeypatch.setattr("cfhe_data.airtable_sync.AirtableClient", FakeClient)
        _snapshot, client = fetch_airtable_snapshot(token="test-token", config=config)
        return client.config

    v2 = fetch(_date_config())
    config = _entitlement_config()
    v3 = fetch(config)
    entitlement_ids = {config["rhna_fields"][band] for band in ENTITLEMENT_BANDS}
    for key in ("managed_field_ids", "writable_field_ids"):
        assert len(v3[key]) == len(v2[key]) + 4
        assert set(v3[key]) == set(v2[key]) | entitlement_ids
    assert clients[0].config == clients[2].config


def test_review_validator_accepts_entitlements_only_as_a_complete_set() -> None:
    _metadata, rows = _validate_source(_source(), entitlements=None)
    assert "entitlement_total" not in rows[0]
    metadata, rows = _validate_source(_add_entitlements(_source()), entitlements=None)
    assert metadata["entitlement_selected_units"] == 12
    assert rows[0]["entitlement_total"] == 10
    assert rows[1]["entitlement_data_status"] == "no_selected_rows"
    partial = _add_entitlements(_source())
    del partial["jurisdictions"][1]["entitlement_total"]
    with pytest.raises(AirtableSyncError, match="row 1 fields changed"):
        _validate_source(partial, entitlements=None)
    malformed = _add_entitlements(_source())
    malformed["jurisdictions"][0]["entitlement_low"] = -3
    with pytest.raises(AirtableSyncError, match="nonnegative integer"):
        _validate_source(malformed, entitlements=None)


def test_committed_config_accepts_the_committed_totals() -> None:
    # The publish job reads this exact pair, and the refresh runs this suite on
    # the artifact it just built. Version 3 requires the entitlement fields and
    # versions 1 and 2 reject them, so a config and artifact that disagree fail
    # here before a merge instead of in the publish job after one.
    config = json.loads(
        (ROOT / "config/airtable_sync.json").read_text(encoding="utf-8")
    )
    totals = json.loads(
        (ROOT / "data/processed/airtable_totals.json").read_text(encoding="utf-8")
    )
    version = normalize_sync_config(config)["config_version"]
    try:
        _validate_source(totals, entitlements=version == 3)
    except AirtableSyncError as error:
        pytest.fail(
            f"config/airtable_sync.json version {version} cannot publish "
            f"data/processed/airtable_totals.json: {error}"
        )
