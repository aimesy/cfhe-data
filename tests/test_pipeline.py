# visibility: public
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import re
from pathlib import Path

import pytest

from cfhe_data import pipeline
from cfhe_data.pipeline import (
    ENTITLEMENT_ACTIVITY,
    ENTITLEMENT_METADATA_FIELDS,
    ENTITLEMENT_ROW_FIELDS,
    PERMIT_ACTIVITY,
    RHNA_NUMERIC_FIELDS,
    build_airtable_artifacts,
    build_artifacts,
    load_planning_periods,
    select_activity_records,
    select_permit_records,
)
from cfhe_data.schema import CsvContract

OUTPUT_SCHEMA = Path(__file__).resolve().parents[1] / "schemas/jurisdiction_totals.json"
AIRTABLE_OUTPUT_SCHEMA = (
    Path(__file__).resolve().parents[1] / "schemas/airtable_totals.json"
)


TABLE_FIELDS = (
    "JURIS_NAME",
    "YEAR",
    "PROJECT_NAME",
    "JURS_TRACKING_ID",
    "UNIT_CAT",
    "TENURE",
    "APN",
    "STREET_ADDRESS",
    "STD_ADDRESS",
    "LATITUDE",
    "LONGITUDE",
    "BP_ISSUE_DT1",
    "NO_BUILDING_PERMITS",
    "BP_ACUTELY_LOW_INCOME_DR",
    "BP_ACUTELY_LOW_INCOME_NDR",
    "BP_EXTREMELY_LOW_INCOME_DR",
    "BP_EXTREMELY_LOW_INCOME_NDR",
    "BP_VLOW_INCOME_DR",
    "BP_VLOW_INCOME_NDR",
    "BP_LOW_INCOME_DR",
    "BP_LOW_INCOME_NDR",
    "BP_MOD_INCOME_DR",
    "BP_MOD_INCOME_NDR",
    "BP_ABOVE_MOD_INCOME",
    "ENT_APPROVE_DT1",
    "NO_ENTITLEMENTS",
    "ACUTELY_LOW_INCOME_DR",
    "ACUTELY_LOW_INCOME_NDR",
    "EXTREMELY_LOW_INCOME_DR",
    "EXTREMELY_LOW_INCOME_NDR",
    "VLOW_INCOME_DR",
    "VLOW_INCOME_NDR",
    "LOW_INCOME_DR",
    "LOW_INCOME_NDR",
    "MOD_INCOME_DR",
    "MOD_INCOME_NDR",
    "ABOVE_MOD_INCOME",
)


def permit_row(**values: str) -> dict[str, str]:
    row = {field: "" for field in TABLE_FIELDS}
    row.update(
        {
            "JURIS_NAME": "Example City",
            "YEAR": "2025",
            "PROJECT_NAME": "Example",
            "JURS_TRACKING_ID": "BP-123",
            "UNIT_CAT": "5+ units",
            "TENURE": "Rental",
            "APN": "123-456-789",
            "STREET_ADDRESS": "10 Main Street",
            "STD_ADDRESS": "10 Main St",
            "LATITUDE": "38.5",
            "LONGITUDE": "-121.5",
            "BP_ISSUE_DT1": "02/01/2024",
            "NO_BUILDING_PERMITS": "1",
            "BP_LOW_INCOME_DR": "1",
        }
    )
    row.update(values)
    return row


def entitlement_row(**values: str) -> dict[str, str]:
    """Return a row that reports one completed entitlement and no permit."""

    return permit_row(
        **{
            "BP_ISSUE_DT1": "",
            "NO_BUILDING_PERMITS": "",
            "BP_LOW_INCOME_DR": "",
            "ENT_APPROVE_DT1": "05/01/2024",
            "NO_ENTITLEMENTS": "1",
            "LOW_INCOME_DR": "1",
            **values,
        }
    )


def write_csv(path: Path, fields: tuple[str, ...], rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


@pytest.mark.parametrize("datastore_change", [None, "reorder", "missing", "extra"])
def test_pipeline_filters_deduplicates_and_marks_undated_rows(
    tmp_path: Path, datastore_change: str | None
) -> None:
    table = tmp_path / "table.csv"
    rhna = tmp_path / "rhna.csv"
    write_csv(
        table,
        TABLE_FIELDS,
        [
            permit_row(YEAR="2024", STREET_ADDRESS="10 Main Street"),
            permit_row(YEAR="2025", STREET_ADDRESS="10 Main St"),
            permit_row(
                JURIS_NAME="Second City",
                YEAR="2025",
                JURS_TRACKING_ID="",
                PROJECT_NAME="",
                APN="",
                STREET_ADDRESS="",
                STD_ADDRESS="",
                BP_ISSUE_DT1="",
                NO_BUILDING_PERMITS="2",
                BP_LOW_INCOME_DR="2",
            ),
            permit_row(
                YEAR="2025",
                JURS_TRACKING_ID="OUTSIDE",
                BP_ISSUE_DT1="01/01/2032",
            ),
        ],
    )
    rhna_fields = (
        "Jurisdiction",
        "Planning Period",
        "6th Cycle Started",
        "VLI UNITS",
        "RHNA VLI",
        "LI UNITS",
        "RHNA LI",
        "MOD UNITS",
        "RHNA MOD",
        "ABOVE MOD UNITS",
        "RHNA ABOVE MOD",
    )
    blank_counts = {field: "0" for field in rhna_fields[3:]}
    write_csv(
        rhna,
        rhna_fields,
        [
            {
                "Jurisdiction": "Example City",
                "Planning Period": "01/01/2023 - 12/31/2031",
                "6th Cycle Started": "TRUE",
                **blank_counts,
            },
            {
                "Jurisdiction": "Second City",
                "Planning Period": "01/01/2023 - 12/31/2031",
                "6th Cycle Started": "TRUE",
                **blank_counts,
            },
            {
                "Jurisdiction": "Example County",
                "Planning Period": "01/01/2023 - 12/31/2031",
                "6th Cycle Started": "TRUE",
                **blank_counts,
            },
        ],
    )
    table_contract = tmp_path / "table-contract.json"
    rhna_contract = tmp_path / "rhna-contract.json"
    table_contract.write_text(
        json.dumps({"required_columns": list(TABLE_FIELDS)}), encoding="utf-8"
    )
    rhna_contract.write_text(
        json.dumps(
            {
                "required_columns": list(rhna_fields),
                "ordered_columns": list(rhna_fields),
            }
        ),
        encoding="utf-8",
    )
    table_bytes = table.read_bytes()
    rhna_bytes = rhna.read_bytes()
    manifest = {
        "manifest_version": 1,
        "sources": {
            "table_a2": {
                "bytes": len(table_bytes),
                "sha256": hashlib.sha256(table_bytes).hexdigest(),
                "md5": hashlib.md5(table_bytes, usedforsecurity=False).hexdigest(),
                "last_modified": "Fri, 14 Aug 2026 00:00:00 GMT",
                "retrieved_at": "2026-08-20T00:00:00+00:00",
            },
            "rhna_progress_6": {
                "bytes": len(rhna_bytes),
                "sha256": hashlib.sha256(rhna_bytes).hexdigest(),
                "md5": hashlib.md5(rhna_bytes, usedforsecurity=False).hexdigest(),
                "datastore_total": 3,
                "datastore_fields": list(rhna_fields),
            },
        },
    }

    datastore_fields = manifest["sources"]["rhna_progress_6"]["datastore_fields"]
    if datastore_change == "reorder":
        datastore_fields.reverse()
    elif datastore_change == "missing":
        datastore_fields.pop()
    elif datastore_change == "extra":
        datastore_fields.append("Unexpected Column")
    if datastore_change is not None:
        with pytest.raises(ValueError, match="RHNA DataStore fields do not match"):
            build_artifacts(
                table_a2_path=table,
                rhna_path=rhna,
                table_contract_path=table_contract,
                rhna_contract_path=rhna_contract,
                output_schema_path=OUTPUT_SCHEMA,
                source_manifest=manifest,
                cutoff_year=2025,
                output_dir=tmp_path / "out",
                audit_dir=tmp_path / "audit",
            )
        assert not (tmp_path / "out").exists()
        return

    artifacts = build_artifacts(
        table_a2_path=table,
        rhna_path=rhna,
        table_contract_path=table_contract,
        rhna_contract_path=rhna_contract,
        output_schema_path=OUTPUT_SCHEMA,
        source_manifest=manifest,
        cutoff_year=2025,
        output_dir=tmp_path / "out",
        audit_dir=tmp_path / "audit",
    )

    payload = json.loads(artifacts.jurisdiction_json.read_text(encoding="utf-8"))
    rows = {row["jurisdiction"]: row for row in payload["jurisdictions"]}
    assert artifacts.selected_rows == 3
    assert artifacts.retained_rows == 2
    assert artifacts.removed_rows == 1
    assert artifacts.total_units == 3
    assert rows["Example City"]["low"] == 1
    assert rows["Second City"]["undated_permits"] == 2
    assert rows["Example County (Unincorporated)"]["data_status"] == "no_selected_rows"
    assert payload["metadata"]["last_updated"] == "2026-08-14"
    assert payload["metadata"]["provisional"] is False
    ledger = artifacts.decision_ledger.read_text(encoding="utf-8").splitlines()
    assert len(ledger) == 3
    assert (
        sum(
            json.loads(line)["decision"] == "removed_earlier_snapshot"
            for line in ledger
        )
        == 1
    )
    review_ledger = artifacts.review_ledger.read_text(encoding="utf-8").splitlines()
    assert len(review_ledger) == 2
    review_rows = [json.loads(line) for line in review_ledger]
    review_row = next(
        row for row in review_rows if row["decision"] == "removed_earlier_snapshot"
    )
    retained_row = next(row for row in review_rows if row["decision"] == "retained")
    assert review_row["decision"] == "removed_earlier_snapshot"
    assert review_row["lineage_resolution"] == "direct"
    assert review_row["matched_source_indices"] == review_row["retained_source_indices"]
    assert review_row["retained_source_indices"] == [
        retained_row["source_record_index"]
    ]
    assert (
        not {
            "raw_row",
            "street_address",
            "standard_address",
            "apn",
            "project_name",
            "tracking_id",
        }
        & review_row.keys()
    )

    first_bytes = {
        path.name: path.read_bytes()
        for path in (
            artifacts.jurisdiction_json,
            artifacts.jurisdiction_csv,
            artifacts.audit_summary,
            artifacts.decision_ledger,
            artifacts.review_ledger,
        )
    }
    repeated = build_artifacts(
        table_a2_path=table,
        rhna_path=rhna,
        table_contract_path=table_contract,
        rhna_contract_path=rhna_contract,
        output_schema_path=OUTPUT_SCHEMA,
        source_manifest=manifest,
        cutoff_year=2025,
        output_dir=tmp_path / "out",
        audit_dir=tmp_path / "audit",
    )
    assert first_bytes == {
        path.name: path.read_bytes()
        for path in (
            repeated.jurisdiction_json,
            repeated.jurisdiction_csv,
            repeated.audit_summary,
            repeated.decision_ledger,
            repeated.review_ledger,
        )
    }


def test_pipeline_rejects_source_bytes_that_do_not_match_manifest(
    tmp_path: Path,
) -> None:
    table = tmp_path / "table.csv"
    rhna = tmp_path / "rhna.csv"
    write_csv(table, TABLE_FIELDS, [permit_row()])
    rhna_fields = (
        "Jurisdiction",
        "Planning Period",
        "6th Cycle Started",
        "VLI UNITS",
        "RHNA VLI",
        "LI UNITS",
        "RHNA LI",
        "MOD UNITS",
        "RHNA MOD",
        "ABOVE MOD UNITS",
        "RHNA ABOVE MOD",
    )
    blank_counts = {field: "0" for field in rhna_fields[3:]}
    write_csv(
        rhna,
        rhna_fields,
        [
            {
                "Jurisdiction": "Example City",
                "Planning Period": "01/01/2023 - 12/31/2031",
                "6th Cycle Started": "TRUE",
                **blank_counts,
            }
        ],
    )
    table_contract = tmp_path / "table-contract.json"
    rhna_contract = tmp_path / "rhna-contract.json"
    table_contract.write_text(
        json.dumps({"required_columns": list(TABLE_FIELDS)}), encoding="utf-8"
    )
    rhna_contract.write_text(
        json.dumps({"required_columns": list(rhna_fields)}), encoding="utf-8"
    )
    table_bytes = table.read_bytes()
    rhna_bytes = rhna.read_bytes()
    manifest = {
        "manifest_version": 1,
        "sources": {
            "table_a2": {
                "bytes": len(table_bytes),
                "sha256": hashlib.sha256(table_bytes).hexdigest(),
            },
            "rhna_progress_6": {
                "bytes": len(rhna_bytes),
                "sha256": hashlib.sha256(rhna_bytes).hexdigest(),
            },
        },
    }

    table.write_bytes(table_bytes.replace(b"BP-123", b"BP-124"))

    try:
        build_artifacts(
            table_a2_path=table,
            rhna_path=rhna,
            table_contract_path=table_contract,
            rhna_contract_path=rhna_contract,
            output_schema_path=OUTPUT_SCHEMA,
            source_manifest=manifest,
            cutoff_year=2025,
            output_dir=tmp_path / "out",
            audit_dir=tmp_path / "audit",
        )
    except ValueError as error:
        assert "SHA-256" in str(error)
    else:
        raise AssertionError("Expected source manifest verification to fail")


def test_airtable_artifact_uses_declared_cycle_per_jurisdiction(
    tmp_path: Path,
) -> None:
    table = tmp_path / "table.csv"
    rhna = tmp_path / "rhna.csv"
    write_csv(
        table,
        TABLE_FIELDS,
        [
            permit_row(
                JURS_TRACKING_ID="EX-2023",
                YEAR="2023",
                BP_ISSUE_DT1="03/01/2023",
            ),
            permit_row(
                JURS_TRACKING_ID="EX-2025",
                YEAR="2025",
                BP_ISSUE_DT1="03/01/2025",
            ),
            permit_row(
                JURIS_NAME="Second City",
                JURS_TRACKING_ID="SECOND-2023",
                YEAR="2023",
                BP_ISSUE_DT1="04/01/2023",
            ),
            permit_row(
                JURIS_NAME="Second City",
                JURS_TRACKING_ID="SECOND-2025",
                YEAR="2025",
                BP_ISSUE_DT1="04/01/2025",
                NO_BUILDING_PERMITS="3",
                BP_LOW_INCOME_DR="3",
            ),
        ],
    )
    rhna_fields = (
        "Jurisdiction",
        "Planning Period",
        "6th Cycle Started",
        "VLI UNITS",
        "RHNA VLI",
        "LI UNITS",
        "RHNA LI",
        "MOD UNITS",
        "RHNA MOD",
        "ABOVE MOD UNITS",
        "RHNA ABOVE MOD",
    )
    blank_counts = {field: "0" for field in rhna_fields[3:]}
    write_csv(
        rhna,
        rhna_fields,
        [
            {
                "Jurisdiction": "Example City",
                "Planning Period": "01/01/2023 - 12/31/2031",
                "6th Cycle Started": "TRUE",
                **blank_counts,
            },
            {
                "Jurisdiction": "Second City",
                "Planning Period": "01/01/2023 - 12/31/2031",
                "6th Cycle Started": "TRUE",
                **blank_counts,
            },
        ],
    )
    table_contract = tmp_path / "table-contract.json"
    rhna_contract = tmp_path / "rhna-contract.json"
    table_contract.write_text(
        json.dumps({"required_columns": list(TABLE_FIELDS)}), encoding="utf-8"
    )
    rhna_contract.write_text(
        json.dumps({"required_columns": list(rhna_fields)}), encoding="utf-8"
    )
    policy = tmp_path / "cycles.json"
    policy.write_text(
        json.dumps(
            {
                "policy_version": 1,
                "default_cycle": "6th",
                "expected_jurisdiction_count": 2,
                "expected_cycle_counts": {"6th": 1, "7th": 1},
                "overrides": [
                    {
                        "jurisdiction_key": "SECOND CITY",
                        "cycle": "7th",
                        "start": "2024-06-30",
                        "end": "2029-06-30",
                    }
                ],
                "source": "https://www.hcd.ca.gov/rhna/seventh-cycle",
            }
        ),
        encoding="utf-8",
    )
    table_bytes = table.read_bytes()
    rhna_bytes = rhna.read_bytes()
    manifest = {
        "manifest_version": 1,
        "sources": {
            "table_a2": {
                "bytes": len(table_bytes),
                "sha256": hashlib.sha256(table_bytes).hexdigest(),
                "last_modified": "Fri, 14 Aug 2026 00:00:00 GMT",
                "retrieved_at": "2026-08-20T00:00:00+00:00",
            },
            "rhna_progress_6": {
                "bytes": len(rhna_bytes),
                "sha256": hashlib.sha256(rhna_bytes).hexdigest(),
            },
        },
    }

    artifacts = build_airtable_artifacts(
        table_a2_path=table,
        rhna_path=rhna,
        table_contract_path=table_contract,
        rhna_contract_path=rhna_contract,
        output_schema_path=AIRTABLE_OUTPUT_SCHEMA,
        cycle_policy_path=policy,
        source_manifest=manifest,
        cutoff_year=2025,
        output_dir=tmp_path / "out",
        audit_dir=tmp_path / "audit",
    )

    payload = json.loads(artifacts.jurisdiction_json.read_text(encoding="utf-8"))
    audit_payload = json.loads(artifacts.audit_summary.read_text(encoding="utf-8"))
    rows = {row["jurisdiction_key"]: row for row in payload["jurisdictions"]}
    assert payload["metadata"]["cycle_counts"] == {"6th": 1, "7th": 1}
    assert audit_payload["removed_by_rule"] == {}
    assert artifacts.review_ledger.read_text(encoding="utf-8") == ""
    assert rows["EXAMPLE CITY"]["cycle"] == "6th"
    assert rows["EXAMPLE CITY"]["total"] == 2
    assert rows["SECOND CITY"]["cycle"] == "7th"
    assert rows["SECOND CITY"]["period_start"] == "2024-06-30"
    assert rows["SECOND CITY"]["total"] == 3

    bad_policy = json.loads(policy.read_text(encoding="utf-8"))
    bad_policy["overrides"].append(dict(bad_policy["overrides"][0]))
    policy.write_text(json.dumps(bad_policy), encoding="utf-8")
    with pytest.raises(ValueError, match="Duplicate Airtable cycle override"):
        build_airtable_artifacts(
            table_a2_path=table,
            rhna_path=rhna,
            table_contract_path=table_contract,
            rhna_contract_path=rhna_contract,
            output_schema_path=AIRTABLE_OUTPUT_SCHEMA,
            cycle_policy_path=policy,
            source_manifest=manifest,
            cutoff_year=2025,
            output_dir=tmp_path / "out",
            audit_dir=tmp_path / "audit",
        )


def _planning_period_fixture(tmp_path: Path, periods: dict[str, str]):
    fields = (
        "Jurisdiction",
        "Planning Period",
        "6th Cycle Started",
        *RHNA_NUMERIC_FIELDS,
    )
    path = tmp_path / "rhna-periods.csv"
    write_csv(
        path,
        fields,
        [
            {
                "Jurisdiction": key,
                "Planning Period": period,
                "6th Cycle Started": "TRUE",
                **dict.fromkeys(RHNA_NUMERIC_FIELDS, "0"),
            }
            for key, period in periods.items()
        ],
    )
    return path, CsvContract(required_columns=fields)


@pytest.mark.parametrize(
    "source_period", ["11/15/2022 - 11/15/2030", "02/15/2023 - 02/15/2031"]
)
def test_siskiyou_correction_matches_verified_official_jurisdictions(
    tmp_path: Path, source_period: str
) -> None:
    provenance_path = (
        OUTPUT_SCHEMA.parents[1] / "config/siskiyou_planning_period_correction.json"
    )
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    official = provenance["official_rows"]
    path, contract = _planning_period_fixture(
        tmp_path,
        {
            **{row["juris_name"]: source_period for row in official},
            "Colusa": "12/31/2020 - 12/31/2028",
            "San Luis Obispo": "12/31/2020 - 12/31/2028",
        },
    )
    periods = load_planning_periods(path, contract)

    assert len(official) == 10
    assert {row["juris_name"].upper() for row in official} == set(
        provenance["jurisdiction_keys"]
    )
    for row in official:
        period = periods[row["juris_name"].upper()]
        assert period.start.isoformat() == row["plan_start_dt"] == "2023-02-15"
        assert period.end.isoformat() == row["plan_end_dt"] == "2031-02-15"
    for key in ("COLUSA", "SAN LUIS OBISPO"):
        assert periods[key].start == dt.date(2020, 12, 31)
        assert periods[key].end == dt.date(2028, 12, 31)


@pytest.mark.parametrize(
    "unreviewed_period", ["02/15/2023 - 02/15/2039", "11/15/2022 - 02/15/2031"]
)
def test_siskiyou_period_correction_rejects_unreviewed_source_dates(
    tmp_path: Path, unreviewed_period: str
) -> None:
    path, contract = _planning_period_fixture(tmp_path, {"Yreka": unreviewed_period})
    with pytest.raises(ValueError, match="Unreviewed Siskiyou planning period"):
        load_planning_periods(path, contract)


def test_siskiyou_permits_use_corrected_start_and_end_boundaries(
    tmp_path: Path,
) -> None:
    path, contract = _planning_period_fixture(
        tmp_path, {"Yreka": "11/15/2022 - 11/15/2030"}
    )
    periods = load_planning_periods(path, contract)
    assert set(periods) == {"YREKA"}
    table = tmp_path / "boundary-permits.csv"
    dates = [
        "2022-11-15",
        "2022-12-31",
        "2023-02-14",
        "2023-02-15",
        "2030-11-15",
        "2031-02-15",
        "2031-02-16",
    ]
    write_csv(
        table,
        TABLE_FIELDS,
        [
            permit_row(
                JURIS_NAME="Yreka",
                JURS_TRACKING_ID=f"BOUNDARY-{index}",
                YEAR=value[:4],
                BP_ISSUE_DT1=value,
            )
            for index, value in enumerate(dates)
        ],
    )

    selected, _stats = select_permit_records(
        table, CsvContract(required_columns=TABLE_FIELDS), periods, 2031
    )

    assert [row.permit_date.isoformat() for row in selected] == [
        "2023-02-15",
        "2030-11-15",
        "2031-02-15",
    ]


def _airtable_inputs(tmp_path: Path, rows: list[dict[str, str]]) -> dict[str, object]:
    """Write Airtable build inputs for Example City (6th) and Second City (7th)."""

    tmp_path.mkdir(parents=True, exist_ok=True)
    table = tmp_path / "table.csv"
    write_csv(table, TABLE_FIELDS, rows)
    rhna, rhna_contract = _planning_period_fixture(
        tmp_path,
        dict.fromkeys(("Example City", "Second City"), "01/01/2023 - 12/31/2031"),
    )
    table_contract = tmp_path / "table-contract.json"
    table_contract.write_text(
        json.dumps({"required_columns": list(TABLE_FIELDS)}), encoding="utf-8"
    )
    rhna_contract_path = tmp_path / "rhna-contract.json"
    rhna_contract_path.write_text(
        json.dumps({"required_columns": list(rhna_contract.required_columns)}),
        encoding="utf-8",
    )
    policy = tmp_path / "cycles.json"
    policy.write_text(
        json.dumps(
            {
                "policy_version": 1,
                "default_cycle": "6th",
                "expected_jurisdiction_count": 2,
                "expected_cycle_counts": {"6th": 1, "7th": 1},
                "overrides": [
                    {
                        "jurisdiction_key": "SECOND CITY",
                        "cycle": "7th",
                        "start": "2024-06-30",
                        "end": "2029-06-30",
                    }
                ],
                "source": "https://www.hcd.ca.gov/rhna/seventh-cycle",
            }
        ),
        encoding="utf-8",
    )
    table_bytes = table.read_bytes()
    rhna_bytes = rhna.read_bytes()
    return {
        "table_a2_path": table,
        "rhna_path": rhna,
        "table_contract_path": table_contract,
        "rhna_contract_path": rhna_contract_path,
        "output_schema_path": AIRTABLE_OUTPUT_SCHEMA,
        "cycle_policy_path": policy,
        "source_manifest": {
            "manifest_version": 1,
            "sources": {
                "table_a2": {
                    "bytes": len(table_bytes),
                    "sha256": hashlib.sha256(table_bytes).hexdigest(),
                    "last_modified": "Fri, 14 Aug 2026 00:00:00 GMT",
                    "retrieved_at": "2026-08-20T00:00:00+00:00",
                },
                "rhna_progress_6": {
                    "bytes": len(rhna_bytes),
                    "sha256": hashlib.sha256(rhna_bytes).hexdigest(),
                },
            },
        },
        "cutoff_year": 2025,
        "output_dir": tmp_path / "out",
        "audit_dir": tmp_path / "audit",
    }


def test_entitlement_bands_sum_unprefixed_columns_only(tmp_path: Path) -> None:
    path, contract = _planning_period_fixture(
        tmp_path, {"Example City": "01/01/2023 - 12/31/2031"}
    )
    periods = load_planning_periods(path, contract)
    fields = (*TABLE_FIELDS, "EXTR_LOW_INCOME_UNITS")
    table = tmp_path / "bands.csv"
    write_csv(
        table,
        fields,
        [
            permit_row(
                ENT_APPROVE_DT1="05/01/2024",
                NO_ENTITLEMENTS="66",
                ACUTELY_LOW_INCOME_DR="1",
                ACUTELY_LOW_INCOME_NDR="2",
                EXTREMELY_LOW_INCOME_DR="3",
                EXTREMELY_LOW_INCOME_NDR="4",
                VLOW_INCOME_DR="5",
                VLOW_INCOME_NDR="6",
                LOW_INCOME_DR="7",
                LOW_INCOME_NDR="8",
                MOD_INCOME_DR="9",
                MOD_INCOME_NDR="10",
                ABOVE_MOD_INCOME="11",
                EXTR_LOW_INCOME_UNITS="4",
            )
        ],
    )

    (permits, _), (entitlements, _) = select_activity_records(
        table,
        CsvContract(required_columns=fields),
        periods,
        2025,
        (PERMIT_ACTIVITY, ENTITLEMENT_ACTIVITY),
    )

    (permit,) = permits
    (entitlement,) = entitlements
    assert "EXTR_LOW_INCOME_UNITS" not in ENTITLEMENT_ACTIVITY.fields
    assert permit.categories == (0, 0, 0, 1, 0, 0)
    assert permit.permit_date == dt.date(2024, 2, 1)
    assert entitlement.categories == (3, 7, 11, 15, 19, 11)
    assert entitlement.units == entitlement.reported_total == 66
    assert entitlement.permit_date == dt.date(2024, 5, 1)


def test_entitlement_selection_mirrors_the_permit_window(tmp_path: Path) -> None:
    path, contract = _planning_period_fixture(
        tmp_path, {"Example City": "01/01/2023 - 12/31/2031"}
    )
    periods = load_planning_periods(path, contract)
    table = tmp_path / "activities.csv"
    write_csv(
        table,
        TABLE_FIELDS,
        [
            permit_row(JURS_TRACKING_ID="PERMIT-ONLY"),
            entitlement_row(JURS_TRACKING_ID="ENTITLEMENT-ONLY"),
            permit_row(
                JURS_TRACKING_ID="BOTH",
                BP_ISSUE_DT1="03/01/2025",
                ENT_APPROVE_DT1="07/01/2023",
                NO_ENTITLEMENTS="1",
                VLOW_INCOME_DR="1",
            ),
            entitlement_row(
                JURS_TRACKING_ID="BEFORE-START",
                YEAR="2023",
                ENT_APPROVE_DT1="12/31/2022",
            ),
            entitlement_row(
                JURS_TRACKING_ID="AT-START", YEAR="2023", ENT_APPROVE_DT1="01/01/2023"
            ),
            entitlement_row(JURS_TRACKING_ID="AT-CUTOFF", ENT_APPROVE_DT1="12/31/2025"),
            entitlement_row(
                JURS_TRACKING_ID="AFTER-CUTOFF",
                YEAR="2026",
                ENT_APPROVE_DT1="01/01/2026",
            ),
            entitlement_row(
                JURS_TRACKING_ID="UNDATED-IN",
                YEAR="2024",
                ENT_APPROVE_DT1="",
                NO_ENTITLEMENTS="2",
                LOW_INCOME_DR="2",
            ),
            entitlement_row(
                JURS_TRACKING_ID="UNDATED-OUT", YEAR="2022", ENT_APPROVE_DT1=""
            ),
        ],
    )
    table_contract = CsvContract(required_columns=TABLE_FIELDS)

    (permits, permit_stats), (entitlements, entitlement_stats) = (
        select_activity_records(
            table,
            table_contract,
            periods,
            2025,
            (PERMIT_ACTIVITY, ENTITLEMENT_ACTIVITY),
        )
    )

    assert [record.tracking_raw for record in permits] == ["PERMIT-ONLY", "BOTH"]
    assert [record.tracking_raw for record in entitlements] == [
        "ENTITLEMENT-ONLY",
        "BOTH",
        "AT-START",
        "AT-CUTOFF",
        "UNDATED-IN",
    ]
    assert [record.permit_date for record in entitlements] == [
        dt.date(2024, 5, 1),
        dt.date(2023, 7, 1),
        dt.date(2023, 1, 1),
        dt.date(2025, 12, 31),
        None,
    ]
    assert entitlements[1].categories == (0, 0, 1, 0, 0, 0)
    assert entitlements[-1].undated
    assert entitlements[-1].units == 2
    assert entitlement_stats["selected_row_count"] == 5
    assert entitlement_stats["max_report_year"] == 2026
    assert select_permit_records(table, table_contract, periods, 2025) == (
        permits,
        permit_stats,
    )


@pytest.mark.parametrize(
    ("values", "message"),
    [
        (
            {"NO_ENTITLEMENTS": "3", "LOW_INCOME_DR": "2"},
            (
                "1 selected Table A2 rows do not reconcile with NO_ENTITLEMENTS; "
                "net category difference -1"
            ),
        ),
        (
            {"LOW_INCOME_DR": "1.5"},
            "LOW_INCOME_DR must be a nonnegative integer at record 2: '1.5'",
        ),
        (
            {"NO_ENTITLEMENTS": "one"},
            "NO_ENTITLEMENTS is not numeric at record 2: 'one'",
        ),
        (
            {"ENT_APPROVE_DT1": "2024-13-45"},
            "ENT_APPROVE_DT1 has an invalid date at record 2: '2024-13-45'",
        ),
    ],
)
def test_entitlement_values_fail_closed(
    tmp_path: Path, values: dict[str, str], message: str
) -> None:
    inputs = _airtable_inputs(
        tmp_path,
        [
            permit_row(
                **{
                    "ENT_APPROVE_DT1": "05/01/2024",
                    "NO_ENTITLEMENTS": "1",
                    "LOW_INCOME_DR": "1",
                    **values,
                }
            )
        ],
    )

    with pytest.raises(ValueError, match=re.escape(message)):
        build_airtable_artifacts(**inputs)

    assert not (tmp_path / "out").exists()
    assert not (tmp_path / "audit").exists()
    periods = load_planning_periods(
        inputs["rhna_path"], CsvContract(required_columns=("Jurisdiction",))
    )
    permits, _stats = select_permit_records(
        inputs["table_a2_path"],
        CsvContract(required_columns=TABLE_FIELDS),
        periods,
        2025,
    )
    assert [record.units for record in permits] == [1]


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        (
            [permit_row(ENT_APPROVE_DT1="2024-13-45", NO_ENTITLEMENTS="0")],
            "ENT_APPROVE_DT1 has an invalid date at record 2: '2024-13-45'",
        ),
        (
            [
                permit_row(),
                permit_row(
                    JURS_TRACKING_ID="OLD",
                    YEAR="2019",
                    BP_ISSUE_DT1="",
                    ENT_APPROVE_DT1="2019-13-45",
                    NO_BUILDING_PERMITS="0",
                    NO_ENTITLEMENTS="0",
                ),
            ],
            "ENT_APPROVE_DT1 has an invalid date at record 3: '2019-13-45'",
        ),
    ],
)
def test_invalid_entitlement_date_fails_closed_on_uncounted_rows(
    tmp_path: Path, rows: list[dict[str, str]], message: str
) -> None:
    inputs = _airtable_inputs(tmp_path, rows)

    with pytest.raises(ValueError, match=re.escape(message)):
        build_airtable_artifacts(**inputs)

    assert not (tmp_path / "out").exists()
    assert not (tmp_path / "audit").exists()


def test_entitlements_deduplicate_on_their_own_approval_date(tmp_path: Path) -> None:
    inputs = _airtable_inputs(
        tmp_path,
        [
            # One permit reported in two snapshots with different approval dates.
            permit_row(
                JURS_TRACKING_ID="PERMIT-REPEAT",
                YEAR="2024",
                ENT_APPROVE_DT1="01/10/2024",
                NO_ENTITLEMENTS="2",
                LOW_INCOME_DR="2",
            ),
            permit_row(
                JURS_TRACKING_ID="PERMIT-REPEAT",
                YEAR="2025",
                ENT_APPROVE_DT1="01/10/2025",
                NO_ENTITLEMENTS="2",
                LOW_INCOME_DR="2",
            ),
            # One entitlement reported twice; only the later snapshot has a permit.
            entitlement_row(
                JURS_TRACKING_ID="ENTITLEMENT-REPEAT",
                YEAR="2024",
                ENT_APPROVE_DT1="03/01/2023",
                NO_ENTITLEMENTS="3",
                LOW_INCOME_DR="3",
            ),
            permit_row(
                JURS_TRACKING_ID="ENTITLEMENT-REPEAT",
                YEAR="2025",
                BP_ISSUE_DT1="02/01/2025",
                ENT_APPROVE_DT1="03/01/2023",
                NO_ENTITLEMENTS="3",
                LOW_INCOME_DR="3",
            ),
        ],
    )

    artifacts = build_airtable_artifacts(**inputs)

    payload = json.loads(artifacts.jurisdiction_json.read_text(encoding="utf-8"))
    rows = {row["jurisdiction_key"]: row for row in payload["jurisdictions"]}
    assert (
        artifacts.selected_rows,
        artifacts.retained_rows,
        artifacts.removed_rows,
    ) == (3, 2, 1)
    assert (
        artifacts.entitlement_selected_rows,
        artifacts.entitlement_retained_rows,
        artifacts.entitlement_removed_rows,
        artifacts.entitlement_total_units,
    ) == (4, 3, 1, 7)
    assert rows["EXAMPLE CITY"]["low"] == rows["EXAMPLE CITY"]["total"] == 2
    assert rows["EXAMPLE CITY"]["entitlement_low"] == 7
    assert payload["metadata"]["entitlement_removed_row_count"] == 1
    assert payload["metadata"]["entitlement_removed_units"] == 3

    permit_ledger = [
        json.loads(line)
        for line in artifacts.decision_ledger.read_text(encoding="utf-8").splitlines()
    ]
    entitlement_ledger = [
        json.loads(line)
        for line in artifacts.entitlement_decision_ledger.read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    assert all("entitlement_date" not in row for row in permit_ledger)
    assert all("permit_date" not in row for row in entitlement_ledger)
    assert [
        (row["source_record_index"], row["permit_date"])
        for row in permit_ledger
        if row["decision"] == "removed_earlier_snapshot"
    ] == [(2, "2024-02-01")]
    assert [
        (row["source_record_index"], row["entitlement_date"], row["cycle"])
        for row in entitlement_ledger
        if row["decision"] == "removed_earlier_snapshot"
    ] == [(4, "2023-03-01", "6th")]
    review_ledger = artifacts.entitlement_review_ledger.read_text(encoding="utf-8")
    assert [
        json.loads(line)["source_record_index"] for line in review_ledger.splitlines()
    ] == [4, 5]

    audit = json.loads(artifacts.entitlement_audit_summary.read_text(encoding="utf-8"))
    assert audit["source_columns"]["date"] == "ENT_APPROVE_DT1"
    assert audit["source_columns"]["total"] == "NO_ENTITLEMENTS"
    assert audit["removed_categories"]["low"] == 3
    assert sum(rule["rows"] for rule in audit["removed_by_rule"].values()) == 1
    assert all(audit["invariants"].values())
    assert audit["metadata"] == payload["metadata"]


def test_carried_entitlement_under_a_new_tracking_id_counts_once(
    tmp_path: Path,
) -> None:
    permit = permit_row()
    carried = [
        entitlement_row(JURS_TRACKING_ID="ENT-2024-01", YEAR="2024"),
        entitlement_row(
            JURS_TRACKING_ID="PL25-0042", YEAR="2025", PROJECT_NAME="Tract 5045"
        ),
    ]
    artifacts = build_airtable_artifacts(
        **_airtable_inputs(tmp_path / "carried", [permit, *carried])
    )
    baseline = build_airtable_artifacts(
        **_airtable_inputs(tmp_path / "baseline", [permit, carried[1]])
    )

    payload = json.loads(artifacts.jurisdiction_json.read_text(encoding="utf-8"))
    baseline_payload = json.loads(
        baseline.jurisdiction_json.read_text(encoding="utf-8")
    )
    row = {item["jurisdiction_key"]: item for item in payload["jurisdictions"]}
    assert (
        artifacts.entitlement_selected_rows,
        artifacts.entitlement_retained_rows,
        artifacts.entitlement_removed_rows,
        artifacts.entitlement_total_units,
    ) == (2, 1, 1, 1)
    assert row["EXAMPLE CITY"]["entitlement_low"] == 1
    assert (artifacts.selected_rows, artifacts.retained_rows) == (1, 1)
    assert row["EXAMPLE CITY"]["low"] == row["EXAMPLE CITY"]["total"] == 1
    assert payload["jurisdictions"] == baseline_payload["jurisdictions"]

    audit = json.loads(artifacts.entitlement_audit_summary.read_text(encoding="utf-8"))
    assert {
        rule: stats["rows"] for rule, stats in audit["removed_by_rule"].items()
    } == {"entitlement_carried_forward_snapshot": 1}
    assert all(audit["invariants"].values())
    entitlement_ledger = [
        json.loads(line)
        for line in artifacts.entitlement_decision_ledger.read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    assert [
        row["source_record_index"]
        for row in entitlement_ledger
        if row["decision"] == "removed_earlier_snapshot"
    ] == [3]


def test_jurisdiction_without_entitlements_reports_zero_entitlement_fields(
    tmp_path: Path,
) -> None:
    inputs = _airtable_inputs(
        tmp_path,
        [
            entitlement_row(
                JURS_TRACKING_ID="EX-UNDATED",
                YEAR="2024",
                ENT_APPROVE_DT1="",
                NO_ENTITLEMENTS="2",
                LOW_INCOME_DR="2",
            ),
            permit_row(
                JURIS_NAME="Second City",
                JURS_TRACKING_ID="SECOND-1",
                BP_ISSUE_DT1="04/01/2025",
            ),
        ],
    )

    artifacts = build_airtable_artifacts(**inputs)

    payload = json.loads(artifacts.jurisdiction_json.read_text(encoding="utf-8"))
    rows = {row["jurisdiction_key"]: row for row in payload["jurisdictions"]}
    example = rows["EXAMPLE CITY"]
    second = rows["SECOND CITY"]
    assert example["data_status"] == "no_selected_rows"
    assert example["total"] == 0
    assert example["entitlement_data_status"] == "reported"
    assert example["entitlement_low"] == example["entitlement_total"] == 2
    assert example["undated_entitlements"] == 2
    assert second["data_status"] == "reported"
    assert second["total"] == 1
    assert {field: second[field] for field in ENTITLEMENT_ROW_FIELDS} == {
        **dict.fromkeys(ENTITLEMENT_ROW_FIELDS[:-1], 0),
        "entitlement_data_status": "no_selected_rows",
    }
    assert all(type(second[field]) is int for field in ENTITLEMENT_ROW_FIELDS[:-1])
    assert payload["metadata"]["undated_entitlements"] == 2
    audit = json.loads(artifacts.entitlement_audit_summary.read_text(encoding="utf-8"))
    assert audit["selection"]["planning_periods_without_selected_rows"] == [
        "SECOND CITY"
    ]


def test_entitlement_merge_leaves_the_permit_projection_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = [
        permit_row(
            JURS_TRACKING_ID="EX-1",
            YEAR="2024",
            ENT_APPROVE_DT1="01/10/2024",
            NO_ENTITLEMENTS="4",
            LOW_INCOME_DR="1",
            ABOVE_MOD_INCOME="3",
        ),
        permit_row(
            JURS_TRACKING_ID="EX-1",
            YEAR="2025",
            ENT_APPROVE_DT1="01/10/2024",
            NO_ENTITLEMENTS="4",
            LOW_INCOME_DR="1",
            ABOVE_MOD_INCOME="3",
        ),
        permit_row(
            JURIS_NAME="Second City",
            JURS_TRACKING_ID="SECOND-1",
            BP_ISSUE_DT1="04/01/2025",
            NO_ENTITLEMENTS="2",
            MOD_INCOME_NDR="2",
        ),
    ]
    blank_rows = [
        {**row, **dict.fromkeys(ENTITLEMENT_ACTIVITY.fields, "")} for row in rows
    ]
    reported_inputs = _airtable_inputs(tmp_path / "reported", rows)
    blank_inputs = _airtable_inputs(tmp_path / "blank", blank_rows)
    # The Airtable build does not verify source bytes, so both builds can share
    # one manifest and therefore one metadata block.
    blank_inputs["source_manifest"] = reported_inputs["source_manifest"]

    reported = build_airtable_artifacts(**reported_inputs)
    blank = build_airtable_artifacts(**blank_inputs)

    def permit_projection(path: Path) -> str:
        payload = json.loads(path.read_text(encoding="utf-8"))
        projection = {
            "metadata": {
                key: value
                for key, value in payload["metadata"].items()
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
        return json.dumps(projection, indent=2, sort_keys=True, ensure_ascii=False)

    def ledger_without_row_hash(path: Path) -> list[dict[str, object]]:
        return [
            {
                key: value
                for key, value in json.loads(line).items()
                if key != "row_sha256"
            }
            for line in path.read_text(encoding="utf-8").splitlines()
        ]

    reported_metadata = json.loads(
        reported.jurisdiction_json.read_text(encoding="utf-8")
    )["metadata"]
    blank_metadata = json.loads(blank.jurisdiction_json.read_text(encoding="utf-8"))[
        "metadata"
    ]
    assert reported_metadata["entitlement_selected_units"] == 10
    assert reported_metadata["entitlement_removed_units"] == 4
    assert blank_metadata["entitlement_selected_units"] == 0
    assert reported.removed_rows == blank.removed_rows == 1
    assert permit_projection(reported.jurisdiction_json) == permit_projection(
        blank.jurisdiction_json
    )
    assert reported.audit_summary.read_bytes() == blank.audit_summary.read_bytes()
    for name in ("decision_ledger", "review_ledger"):
        assert ledger_without_row_hash(
            getattr(reported, name)
        ) == ledger_without_row_hash(getattr(blank, name))

    monkeypatch.setattr(pipeline, "ENTITLEMENT_ROW_FIELDS", ENTITLEMENT_ROW_FIELDS[1:])
    guarded_inputs = _airtable_inputs(tmp_path / "guarded", rows)
    with pytest.raises(AssertionError, match="changed the Airtable permit projection"):
        build_airtable_artifacts(**guarded_inputs)
    assert not (tmp_path / "guarded" / "out").exists()
