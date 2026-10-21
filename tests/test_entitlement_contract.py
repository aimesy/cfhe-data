# visibility: public
"""The Table A2 build and the Airtable sync agree on the entitlement fields."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from test_airtable_sync import _date_config, _entitlement_config, _entitlement_snapshot
from test_pipeline import (
    AIRTABLE_OUTPUT_SCHEMA,
    _airtable_inputs,
    entitlement_row,
    permit_row,
)

from cfhe_data import pipeline
from cfhe_data.airtable_sync import (
    BASE_METADATA_FIELDS,
    BASE_ROW_FIELDS,
    ENTITLEMENT_BANDS,
    ENTITLEMENT_CATEGORY_FIELDS,
    ENTITLEMENT_METADATA_FIELDS,
    ENTITLEMENT_ROW_FIELDS,
    PERMIT_BANDS,
    AirtableSyncError,
    _validate_source,
    build_airtable_sync_plan,
    plan_record_updates,
    summarize_airtable_sync_plan,
)

PLANNING_FIELDS = {"cycle_policy_sha256", "cycle_counts"}
PERIOD_FIELDS = {"cycle", "period_start", "period_end"}


def test_schema_pipeline_and_sync_name_the_same_entitlement_fields() -> None:
    schema = json.loads(AIRTABLE_OUTPUT_SCHEMA.read_text(encoding="utf-8"))
    metadata = schema["properties"]["metadata"]
    row = schema["properties"]["jurisdictions"]["items"]
    expected_metadata = (
        BASE_METADATA_FIELDS | PLANNING_FIELDS | ENTITLEMENT_METADATA_FIELDS
    )
    expected_row = BASE_ROW_FIELDS | PERIOD_FIELDS | ENTITLEMENT_ROW_FIELDS
    assert set(metadata["required"]) == set(metadata["properties"])
    assert set(metadata["properties"]) == expected_metadata
    assert set(row["required"]) == set(row["properties"]) == expected_row
    assert metadata["additionalProperties"] is row["additionalProperties"] is False

    # The pipeline writes these fields in this order; the sync reads them as sets.
    assert len(set(pipeline.ENTITLEMENT_METADATA_FIELDS)) == len(
        pipeline.ENTITLEMENT_METADATA_FIELDS
    )
    assert set(pipeline.ENTITLEMENT_METADATA_FIELDS) == ENTITLEMENT_METADATA_FIELDS
    assert len(set(pipeline.ENTITLEMENT_ROW_FIELDS)) == len(
        pipeline.ENTITLEMENT_ROW_FIELDS
    )
    assert set(pipeline.ENTITLEMENT_ROW_FIELDS) == ENTITLEMENT_ROW_FIELDS
    assert pipeline.ENTITLEMENT_ROW_FIELDS[:6] == ENTITLEMENT_CATEGORY_FIELDS

    counts = {"type": "integer", "minimum": 0}
    for field in ENTITLEMENT_METADATA_FIELDS:
        assert metadata["properties"][field] == counts
    for field in ENTITLEMENT_ROW_FIELDS - {"entitlement_data_status"}:
        assert row["properties"][field] == counts
    status = {"enum": ["reported", "no_selected_rows"]}
    assert row["properties"]["entitlement_data_status"] == status
    assert row["properties"]["data_status"] == status


def test_built_artifact_drives_a_version_three_plan(tmp_path: Path) -> None:
    inputs = _airtable_inputs(
        tmp_path,
        [
            permit_row(
                JURS_TRACKING_ID="EX-PERMIT",
                PROJECT_NAME="Permit Only",
                NO_BUILDING_PERMITS="3",
                BP_VLOW_INCOME_NDR="2",
            ),
            # A permit and its earlier entitlement reported on one row.
            permit_row(
                JURS_TRACKING_ID="EX-BOTH",
                PROJECT_NAME="Both",
                BP_ISSUE_DT1="03/01/2025",
                BP_LOW_INCOME_DR="",
                BP_ABOVE_MOD_INCOME="1",
                ENT_APPROVE_DT1="07/01/2023",
                NO_ENTITLEMENTS="6",
                ACUTELY_LOW_INCOME_DR="1",
                EXTREMELY_LOW_INCOME_NDR="2",
                VLOW_INCOME_DR="3",
            ),
            # One entitlement reported in two annual reports.
            *(
                entitlement_row(
                    JURS_TRACKING_ID="EX-ENT",
                    PROJECT_NAME="Entitlement Only",
                    YEAR=year,
                    NO_ENTITLEMENTS="4",
                    LOW_INCOME_DR="",
                    LOW_INCOME_NDR="1",
                    MOD_INCOME_DR="1",
                    ABOVE_MOD_INCOME="2",
                )
                for year in ("2024", "2025")
            ),
            entitlement_row(
                JURS_TRACKING_ID="EX-UNDATED",
                YEAR="2024",
                ENT_APPROVE_DT1="",
                NO_ENTITLEMENTS="2",
                LOW_INCOME_DR="2",
            ),
            # Approved after the reporting cutoff.
            entitlement_row(
                JURS_TRACKING_ID="EX-LATE",
                YEAR="2026",
                ENT_APPROVE_DT1="01/01/2026",
                NO_ENTITLEMENTS="5",
                LOW_INCOME_DR="5",
            ),
            # The entitlement predates the seventh cycle; the permit does not.
            permit_row(
                JURIS_NAME="Second City",
                JURS_TRACKING_ID="SECOND-1",
                BP_ISSUE_DT1="04/01/2025",
                NO_BUILDING_PERMITS="2",
                BP_LOW_INCOME_DR="",
                BP_MOD_INCOME_NDR="2",
                ENT_APPROVE_DT1="01/15/2024",
                NO_ENTITLEMENTS="3",
                ABOVE_MOD_INCOME="3",
            ),
        ],
    )

    artifacts = pipeline.build_airtable_artifacts(**inputs)

    payload = json.loads(artifacts.jurisdiction_json.read_text(encoding="utf-8"))
    rows = {row["jurisdiction_key"]: row for row in payload["jurisdictions"]}
    assert {
        field: rows["EXAMPLE CITY"][field]
        for field in (*pipeline.CATEGORY_NAMES, "total")
    } == {
        "acutely_low": 0,
        "extremely_low": 0,
        "very_low": 2,
        "low": 1,
        "moderate": 0,
        "above_moderate": 1,
        "total": 4,
    }
    assert {
        field: rows["EXAMPLE CITY"][field] for field in pipeline.ENTITLEMENT_ROW_FIELDS
    } == {
        "entitlement_acutely_low": 1,
        "entitlement_extremely_low": 2,
        "entitlement_very_low": 3,
        "entitlement_low": 3,
        "entitlement_moderate": 1,
        "entitlement_above_moderate": 2,
        "entitlement_total": 12,
        "undated_entitlements": 2,
        "entitlement_data_status": "reported",
    }
    assert rows["SECOND CITY"]["moderate"] == rows["SECOND CITY"]["total"] == 2
    assert {
        field: rows["SECOND CITY"][field] for field in pipeline.ENTITLEMENT_ROW_FIELDS
    } == {
        **dict.fromkeys(pipeline.ENTITLEMENT_ROW_FIELDS[:-1], 0),
        "entitlement_data_status": "no_selected_rows",
    }
    metadata = payload["metadata"]
    assert {
        field: metadata[field]
        for field in (
            "selected_row_count",
            "selected_units",
            "retained_row_count",
            "removed_row_count",
            "removed_units",
            "undated_permits",
        )
    } == {
        "selected_row_count": 3,
        "selected_units": 6,
        "retained_row_count": 3,
        "removed_row_count": 0,
        "removed_units": 0,
        "undated_permits": 0,
    }
    assert {
        field: metadata[field] for field in pipeline.ENTITLEMENT_METADATA_FIELDS
    } == {
        "entitlement_selected_row_count": 4,
        "entitlement_selected_units": 16,
        "entitlement_retained_row_count": 3,
        "entitlement_removed_row_count": 1,
        "entitlement_removed_units": 4,
        "undated_entitlements": 2,
    }

    _validate_source(payload, entitlements=True)
    with pytest.raises(AirtableSyncError, match="metadata fields changed"):
        _validate_source(payload)

    cycle_policy = json.loads(inputs["cycle_policy_path"].read_text(encoding="utf-8"))
    config = _entitlement_config()
    snapshot = _entitlement_snapshot()
    snapshot["jurisdictions"][1] = {
        "record_id": "recSecondCity00001",
        "name": "Second City",
        "unincorporated": False,
    }
    city_record, second_record = snapshot["rhna_records"]
    second_record["jurisdiction_record_ids"] = ["recSecondCity00001"]
    city_record["entitlement_values"] = {
        "entitlement_vli": None,
        "entitlement_li": 5,
        "entitlement_mi": 1,
        "entitlement_ami": None,
    }
    second_record["entitlement_values"] = {
        "entitlement_vli": 0,
        "entitlement_li": None,
        "entitlement_mi": 0,
        "entitlement_ami": 0,
    }

    plan = build_airtable_sync_plan(
        totals=payload,
        snapshot=snapshot,
        config=config,
        cycle_policy=cycle_policy,
        git_sha="a" * 40,
    )

    assert plan["apply_eligible"] is True
    assert plan["intent"] == (
        "update_existing_permit_entitlement_and_apr_date_fields_only"
    )
    rf = config["rhna_fields"]

    def bands(fields: dict[str, object], names: tuple[str, ...]) -> dict[str, object]:
        return {name: fields[rf[name]] for name in names if rf[name] in fields}

    updates = {update.record_id: update for update in plan_record_updates(plan)}
    city = updates["recExampleRhna0001"]
    second = updates["recCountyRhna00001"]
    assert bands(city.expected_fields, PERMIT_BANDS) == {"vli": 0, "li": None, "ami": 0}
    assert bands(city.desired_fields, PERMIT_BANDS) == {"vli": 2, "li": 1, "ami": 1}
    assert bands(city.expected_fields, ENTITLEMENT_BANDS) == {
        "entitlement_vli": None,
        "entitlement_li": 5,
        "entitlement_ami": None,
    }
    assert bands(city.desired_fields, ENTITLEMENT_BANDS) == {
        "entitlement_vli": 6,
        "entitlement_li": 3,
        "entitlement_ami": 2,
    }
    assert bands(second.desired_fields, PERMIT_BANDS) == {"mi": 2}
    assert bands(second.expected_fields, ENTITLEMENT_BANDS) == {"entitlement_li": None}
    assert bands(second.desired_fields, ENTITLEMENT_BANDS) == {"entitlement_li": 0}
    for update in (city, second):
        for value in bands(update.desired_fields, ENTITLEMENT_BANDS).values():
            assert type(value) is int
    changes = {change["airtable_record_id"]: change for change in plan["changes"]}
    assert (
        changes["recExampleRhna0001"]["after_entitlement_total"],
        changes["recExampleRhna0001"]["undated_entitlements"],
        changes["recCountyRhna00001"]["entitlement_data_status"],
    ) == (12, 2, "no_selected_rows")
    assert plan["source_aggregate_after"] == {
        "vli": 2,
        "li": 1,
        "mi": 2,
        "ami": 1,
        "total": 6,
    }
    assert plan["airtable_entitlement_aggregate_before"] == {
        "entitlement_vli": 0,
        "entitlement_li": 5,
        "entitlement_mi": 1,
        "entitlement_ami": 0,
        "total": 6,
    }
    assert plan["source_entitlement_aggregate_after"] == {
        "entitlement_vli": 6,
        "entitlement_li": 3,
        "entitlement_mi": 1,
        "entitlement_ami": 2,
        "total": 12,
    }
    summary = summarize_airtable_sync_plan(plan)
    assert (summary["source_total"], summary["source_entitlement_total"]) == (6, 12)

    # The permit and date writes are the ones version two plans from the permit
    # projection of the same artifact.
    permit_totals = {
        "metadata": {
            key: value
            for key, value in metadata.items()
            if key not in ENTITLEMENT_METADATA_FIELDS
        },
        "jurisdictions": [
            {
                key: value
                for key, value in row.items()
                if key not in ENTITLEMENT_ROW_FIELDS
            }
            for row in payload["jurisdictions"]
        ],
    }
    permit_snapshot = copy.deepcopy(snapshot)
    for record in permit_snapshot["rhna_records"]:
        del record["entitlement_values"]
    permit_plan = build_airtable_sync_plan(
        totals=permit_totals,
        snapshot=permit_snapshot,
        config=_date_config(),
        cycle_policy=cycle_policy,
        git_sha="a" * 40,
    )
    entitlement_ids = {rf[band] for band in ENTITLEMENT_BANDS}

    def without_entitlements(fields: dict[str, object]) -> dict[str, object]:
        return {
            key: value for key, value in fields.items() if key not in entitlement_ids
        }

    assert [
        (
            update.record_id,
            without_entitlements(update.expected_fields),
            without_entitlements(update.desired_fields),
        )
        for update in plan_record_updates(plan)
    ] == [
        (update.record_id, update.expected_fields, update.desired_fields)
        for update in plan_record_updates(permit_plan)
    ]
    for key in ("airtable_aggregate_before", "source_aggregate_after", "cycle_counts"):
        assert plan[key] == permit_plan[key]
