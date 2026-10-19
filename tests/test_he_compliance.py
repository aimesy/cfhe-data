# visibility: public
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from cfhe_data.airtable_sync import (
    AirtableSyncConfigurationError,
    AirtableSyncSnapshotError,
    AirtableSyncStateChangedError,
)
from cfhe_data.he_compliance import (
    JURISDICTION_FIELD_TYPES,
    RHNA_FIELD_TYPES,
    SOURCE_HEADER,
    airtable_key,
    build_plan,
    fetch_compliance_source,
    parse_compliance_csv,
    plan_record_updates,
    project_snapshot,
    verify_plan_unchanged,
)
from cfhe_data.webflow import canonical_sha256

ROOT = Path(__file__).resolve().parents[1]
CONFIG = json.loads(
    (ROOT / "config/he_compliance_sync.json").read_text(encoding="utf-8")
)
CONFIG["expected_jurisdiction_count"] = 4
CONFIG["max_out_of_compliance_share"] = 0.5
CONFIG["jurisdiction_field_options_sha256"] = {
    name: canonical_sha256(None) for name in CONFIG["jurisdiction_fields"]
}
CONFIG["rhna_field_options_sha256"] = {
    name: canonical_sha256(None) for name in CONFIG["rhna_fields"]
}
VALUES = CONFIG["compliance_values"]
JF = CONFIG["jurisdiction_fields"]
RF = CONFIG["rhna_fields"]


def _csv(rows: list[tuple[str, str, str, str]], *, filler: int = 500) -> bytes:
    lines = [",".join(SOURCE_HEADER)]
    for name, status, reviewed, cycle in rows:
        lines.append(
            f"{name},County,6L,Adopted,2024-01-01 00:00:00.0000000,In,"
            f"{reviewed},{status},COG,{cycle}"
        )
    for index in range(filler):
        lines.append(
            f"FILLER {index},County,6L,Adopted,,In,"
            "2024-01-01 00:00:00.0000000,In,COG,6th Cycle"
        )
    return ("﻿" + "\n".join(lines) + "\n").encode("utf-8")


SOURCE = _csv(
    [
        ("SAN JOSE", "In", "2026-03-23 12:00:00.0000000", "6th Cycle"),
        ("BRISBANE", "Enforcement Out", "2023-05-25 00:00:00.0000000", "6th Cycle"),
        ("SONORA", "In", "2025-01-02 00:00:00.0000000", "7th Cycle"),
        ("ALAMEDA COUNTY", "", "", "6th Cycle"),
    ]
)


def _rows() -> dict[str, dict[str, str | None]]:
    rows = parse_compliance_csv(SOURCE, compliance_values=VALUES)
    return {key: value for key, value in rows.items() if not key.startswith("FILL")}


def _schema(table_id: str, fields: dict[str, str], types: dict[str, str]) -> dict:
    return {
        "complete": True,
        "table_id": table_id,
        "fields": [
            {"id": field_id, "type": types[name], "options": None}
            for name, field_id in fields.items()
        ],
    }


def _export(table_id: str, records: list[tuple[str, dict]]) -> dict:
    return {
        "complete": True,
        "terminal_offset_reached": True,
        "base_id": CONFIG["base_id"],
        "table_id": table_id,
        "record_count": len(records),
        "records": [{"id": rid, "fields": fields} for rid, fields in records],
    }


def _snapshot(rhna_overrides: dict[str, dict] | None = None) -> dict:
    jurisdictions = [
        ("recJ1", {JF["name"]: "San José", JF["unincorporated"]: False}),
        ("recJ2", {JF["name"]: "Brisbane"}),
        ("recJ3", {JF["name"]: "Sonora"}),
        (
            "recJ4",
            {
                JF["name"]: "Alameda County (Unincorporated Areas)",
                JF["unincorporated"]: True,
            },
        ),
        ("recJ5", {JF["name"]: "Sitekick City"}),
    ]
    rhna = {
        "recR1": {
            RF["jurisdiction_link"]: ["recJ1"],
            RF["cycle"]: ["6th"],
            RF["current"]: 1,
            RF["he_compliance"]: "Out of Compliance",
            RF["review_date"]: "2024-07-05",
        },
        "recR1old": {
            RF["jurisdiction_link"]: ["recJ1"],
            RF["cycle"]: ["5th"],
            RF["current"]: 0,
            RF["he_compliance"]: "In Compliance",
        },
        "recR2": {
            RF["jurisdiction_link"]: ["recJ2"],
            RF["cycle"]: ["6th"],
            RF["current"]: 1,
            RF["he_compliance"]: "In Compliance",
            RF["review_date"]: "2022-01-01",
        },
        "recR3": {
            RF["jurisdiction_link"]: ["recJ3"],
            RF["cycle"]: ["7th"],
            RF["current"]: 1,
            RF["he_compliance"]: "In Compliance",
            RF["review_date"]: "2024-12-01",
        },
        "recR4": {
            RF["jurisdiction_link"]: ["recJ4"],
            RF["cycle"]: ["6th"],
            RF["current"]: 1,
            RF["he_compliance"]: "Out of Compliance",
        },
    }
    for record_id, fields in (rhna_overrides or {}).items():
        rhna[record_id] = {**rhna.get(record_id, {}), **fields}
    return project_snapshot(
        config=CONFIG,
        jurisdiction_schema=_schema(
            CONFIG["jurisdictions_table_id"], JF, JURISDICTION_FIELD_TYPES
        ),
        rhna_schema=_schema(CONFIG["rhna_table_id"], RF, RHNA_FIELD_TYPES),
        jurisdiction_export=_export(CONFIG["jurisdictions_table_id"], jurisdictions),
        rhna_export=_export(CONFIG["rhna_table_id"], list(rhna.items())),
    )


def _plan(snapshot: dict | None = None, rows: dict | None = None) -> dict:
    return build_plan(
        source_rows=rows if rows is not None else _rows(),
        source_manifest={"sha256": "x"},
        snapshot=snapshot if snapshot is not None else _snapshot(),
        config=CONFIG,
    )


def test_parse_folds_accents_dates_and_cycles() -> None:
    rows = _rows()
    assert rows["SAN JOSE"]["reviewed_date"] == "2026-03-23"
    assert rows["SONORA"]["cycle"] == "7th"
    assert rows["ALAMEDA COUNTY"]["compliance_status"] == ""
    assert airtable_key("San José") == "SAN JOSE"
    assert airtable_key("Monterey County (Unincorporated Areas)") == "MONTEREY COUNTY"


def test_parse_rejects_header_change_duplicates_and_unknown_status() -> None:
    with pytest.raises(AirtableSyncSnapshotError, match="header"):
        parse_compliance_csv(b"Jurisdiction,Status\n", compliance_values=VALUES)
    duplicate = _csv([("BRISBANE", "In", "", "6th Cycle")] * 2)
    with pytest.raises(AirtableSyncSnapshotError, match="repeats"):
        parse_compliance_csv(duplicate, compliance_values=VALUES)
    unknown = _csv([("BRISBANE", "Pending", "", "6th Cycle")])
    with pytest.raises(AirtableSyncSnapshotError, match="unknown compliance"):
        parse_compliance_csv(unknown, compliance_values=VALUES)
    with pytest.raises(AirtableSyncSnapshotError, match="expected range"):
        parse_compliance_csv(_csv([], filler=10), compliance_values=VALUES)


def test_plan_flips_compliance_and_holds_blank_status() -> None:
    plan = _plan()
    assert plan["apply_eligible"] is True
    assert plan["matched_count"] == 4
    changes = {change["jurisdiction"]: change for change in plan["changes"]}
    assert set(changes) == {"San José", "Brisbane"}
    assert changes["San José"]["after"] == {
        "he_compliance": "In Compliance",
        "review_date": "2026-03-23",
    }
    assert changes["Brisbane"]["after"]["he_compliance"] == "Out of Compliance"
    assert plan["holds"] == [
        {
            "jurisdiction": "Alameda County (Unincorporated Areas)",
            "reason": "HCD status blank",
        }
    ]


def test_unchanged_compliance_keeps_airtable_review_date() -> None:
    plan = _plan()
    assert "Sonora" not in {change["jurisdiction"] for change in plan["changes"]}


def test_cycle_disagreement_is_held_not_written() -> None:
    plan = _plan(_snapshot({"recR3": {RF["cycle"]: ["6th"]}}))
    assert {
        "jurisdiction": "Sonora",
        "reason": "Airtable current cycle 6th but HCD reports 7th",
    } in plan["holds"]


def test_missing_or_extra_jurisdictions_block() -> None:
    rows = _rows()
    del rows["BRISBANE"]
    plan = _plan(rows=rows)
    assert plan["apply_eligible"] is False
    assert "Brisbane is not in the HCD report" in plan["blockers"]
    rows = {**_rows(), "NEW CITY": dict(_rows()["SONORA"])}
    assert (
        "HCD jurisdiction NEW CITY is not in Airtable" in _plan(rows=rows)["blockers"]
    )


def test_implausible_out_share_blocks() -> None:
    rows = copy.deepcopy(_rows())
    for key in ("SAN JOSE", "SONORA"):
        rows[key]["compliance_status"] = "Out"
    plan = _plan(rows=rows)
    assert plan["apply_eligible"] is False
    assert any("plausibility" in blocker for blocker in plan["blockers"])


def test_record_updates_are_conditional_on_reviewed_values() -> None:
    plan = _plan()
    updates = {update.record_id: update for update in plan_record_updates(plan, CONFIG)}
    assert set(updates) == {"recR1", "recR2"}
    assert updates["recR1"].expected_fields == {
        RF["he_compliance"]: "Out of Compliance",
        RF["review_date"]: "2024-07-05",
    }
    assert updates["recR1"].desired_fields[RF["he_compliance"]] == "In Compliance"


def test_state_change_between_plan_and_apply_is_rejected() -> None:
    plan = _plan()
    fresh = _plan(_snapshot({"recR1": {RF["he_compliance"]: "In Compliance"}}))
    with pytest.raises(AirtableSyncStateChangedError):
        verify_plan_unchanged(plan, fresh)


def test_config_rejects_extra_keys() -> None:
    bad = {**CONFIG, "extra": True}
    with pytest.raises(AirtableSyncConfigurationError):
        _plan_config(bad)


def _plan_config(config: dict) -> None:
    build_plan(
        source_rows=_rows(), source_manifest={}, snapshot=_snapshot(), config=config
    )


def test_fetch_source_resolves_pinned_resource() -> None:
    calls: list[str] = []

    def get(url: str) -> bytes:
        calls.append(url)
        if "package_show" in url:
            return json.dumps(
                {
                    "success": True,
                    "result": {
                        "resources": [
                            {
                                "id": CONFIG["source"]["resource_id"],
                                "url": "https://example.test/he.csv",
                                "last_modified": "2026-10-16T15:48:55",
                            }
                        ]
                    },
                }
            ).encode()
        return SOURCE

    data, manifest = fetch_compliance_source(CONFIG["source"], get=get)
    assert data == SOURCE
    assert manifest["url"] == "https://example.test/he.csv"
    assert len(manifest["sha256"]) == 64
    assert calls[1] == "https://example.test/he.csv"
