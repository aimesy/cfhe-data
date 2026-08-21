from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

from cfhe_data.pipeline import build_artifacts

OUTPUT_SCHEMA = Path(__file__).resolve().parents[1] / "schemas/jurisdiction_totals.json"


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


def write_csv(path: Path, fields: tuple[str, ...], rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def test_pipeline_filters_deduplicates_and_marks_undated_rows(tmp_path: Path) -> None:
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
                "md5": hashlib.md5(table_bytes, usedforsecurity=False).hexdigest(),
                "last_modified": "Fri, 14 Aug 2026 00:00:00 GMT",
                "retrieved_at": "2026-08-20T00:00:00+00:00",
            },
            "rhna_progress_6": {
                "bytes": len(rhna_bytes),
                "sha256": hashlib.sha256(rhna_bytes).hexdigest(),
                "md5": hashlib.md5(rhna_bytes, usedforsecurity=False).hexdigest(),
            },
        },
    }

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

    first_bytes = {
        path.name: path.read_bytes()
        for path in (
            artifacts.jurisdiction_json,
            artifacts.jurisdiction_csv,
            artifacts.audit_summary,
            artifacts.decision_ledger,
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
