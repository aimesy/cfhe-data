# visibility: public
from __future__ import annotations

import copy
import datetime as dt
import json
import re
from pathlib import Path

import pytest

from cfhe_data.airtable_sync import (
    JURISDICTION_FIELD_TYPES,
    AirtableSyncBlockedError,
    AirtableSyncConfigurationError,
    AirtableSyncCoverageError,
    AirtableSyncError,
    AirtableSyncSnapshotError,
    AirtableSyncStateChangedError,
)
from cfhe_data.series import SERIES_COLUMNS
from cfhe_data.series_sync import (
    SERIES_FIELD_NAMES,
    SERIES_FIELD_TYPES,
    build_plan,
    normalize_config,
    plan_record_creates,
    plan_record_updates,
    project_snapshot,
    require_complete,
    require_eligible,
    summarize_plan,
    validate_series,
    verify_plan_unchanged,
)
from cfhe_data.webflow import canonical_sha256

ROOT = Path(__file__).resolve().parents[1]
LIVE_CONFIG = json.loads(
    (ROOT / "config/airtable_series_sync.json").read_text(encoding="utf-8")
)
CONFIG = copy.deepcopy(LIVE_CONFIG)
CONFIG["expected_jurisdiction_count"] = 2
CONFIG["jurisdiction_field_options_sha256"] = {
    name: canonical_sha256(None) for name in CONFIG["jurisdiction_fields"]
}
CONFIG["series_field_options_sha256"] = {
    name: canonical_sha256(None) for name in CONFIG["series_fields"]
}
JF = CONFIG["jurisdiction_fields"]
SF = CONFIG["series_fields"]
JURISDICTIONS = (
    ("San Jose", "SAN JOSE"),
    ("Alameda County (Unincorporated Areas)", "ALAMEDA COUNTY"),
)
LINKS = {"SAN JOSE": "recJ1", "ALAMEDA COUNTY": "recJ2"}
JURISDICTION_RECORDS = [
    ("recJ1", {JF["name"]: "San Jose", JF["unincorporated"]: False}),
    (
        "recJ2",
        {
            JF["name"]: "Alameda County (Unincorporated Areas)",
            JF["unincorporated"]: True,
        },
    ),
    ("recJ5", {JF["name"]: "Sitekick City"}),
]


def _row(name: str, key: str, quarter: int, **values: object) -> list[object]:
    row: dict[str, object] = {column: 0 for column in SERIES_COLUMNS}
    row.update(
        key=f"{name} 2018 Q{quarter}",
        jurisdiction=name,
        jurisdiction_key=key,
        year=2018,
        quarter=quarter,
        quarter_start=dt.date(2018, 3 * quarter - 2, 1).isoformat(),
        data_flags="",
    )
    row.update(values)
    return [row[column] for column in SERIES_COLUMNS]


def _series(
    changes: dict[tuple[str, int], dict[str, object]] | None = None,
    *,
    blockers: tuple[str, ...] = (),
) -> dict[str, object]:
    rows = [
        _row(name, key, quarter, **(changes or {}).get((key, quarter), {}))
        for name, key in JURISDICTIONS
        for quarter in range(1, 5)
    ]
    return {
        "columns": list(SERIES_COLUMNS),
        "metadata": {
            "cutoff_year": 2018,
            "last_updated": "2026-10-21",
            "source_manifest_sha256": "a" * 64,
            "row_count": len(rows),
            "jurisdiction_count": len(JURISDICTIONS),
            "quarters_per_jurisdiction": 4,
            "window_start": "2018-01-01",
            "window_end": "2018-12-31",
            "sync_blockers": list(blockers),
        },
        "rows": rows,
    }


def _airtable_fields(row: list[object]) -> dict[str, object]:
    values = dict(zip(SERIES_COLUMNS, row, strict=True))
    fields = {
        SF[name]: values[name]
        for name in SERIES_FIELD_NAMES
        if name not in {"jurisdiction", "data_flags"}
    }
    fields[SF["jurisdiction"]] = [LINKS[str(values["jurisdiction_key"])]]
    if values["data_flags"]:
        fields[SF["data_flags"]] = values["data_flags"]
    return fields


def _synced(series: dict[str, object] | None = None) -> list[tuple[str, dict]]:
    rows = (series or _series())["rows"]
    return [(f"recS{index}", _airtable_fields(row)) for index, row in enumerate(rows)]


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


def _snapshot(
    records: list[tuple[str, dict]],
    jurisdictions: list[tuple[str, dict]] | None = None,
    *,
    series_types: dict[str, str] | None = None,
) -> dict:
    return project_snapshot(
        config=CONFIG,
        jurisdiction_schema=_schema(
            CONFIG["jurisdictions_table_id"], JF, JURISDICTION_FIELD_TYPES
        ),
        series_schema=_schema(
            CONFIG["series_table_id"], SF, series_types or SERIES_FIELD_TYPES
        ),
        jurisdiction_export=_export(
            CONFIG["jurisdictions_table_id"],
            JURISDICTION_RECORDS if jurisdictions is None else jurisdictions,
        ),
        series_export=_export(CONFIG["series_table_id"], records),
    )


def _plan(records: list[tuple[str, dict]], series: dict | None = None, **kw) -> dict:
    return build_plan(
        series=series or _series(), snapshot=_snapshot(records, **kw), config=CONFIG
    )


def test_live_config_maps_every_series_field() -> None:
    config = normalize_config(LIVE_CONFIG)
    assert set(config["series_fields"]) == set(SERIES_FIELD_NAMES)
    assert config["excluded_jurisdictions"] == ["Sitekick City"]
    assert "jurisdiction_key" not in SERIES_FIELD_NAMES


def test_first_plan_fills_blank_records_then_creates_the_rest() -> None:
    plan = _plan([("recB0", {}), ("recB1", {}), ("recB2", {})])
    assert plan["apply_eligible"] is True
    assert plan["matched_count"] == 0
    assert plan["reused_blank_count"] == 3
    assert plan["update_count"] == 0
    assert plan["create_count"] == 5
    assert plan["change_count"] == 8
    assert plan["unused_blank_count"] == 0
    reused = plan["updates"][0]
    assert reused["record_id"] == "recB0"
    assert reused["key"] == "San Jose 2018 Q1"
    assert reused["reused_blank"] is True
    assert set(reused["before"].values()) == {None}
    assert reused["after"]["jurisdiction"] == ["recJ1"]
    assert "data_flags" not in reused["after"]
    assert [fields["key"] for fields in plan["creates"]] == [
        "San Jose 2018 Q4",
        "Alameda County (Unincorporated Areas) 2018 Q1",
        "Alameda County (Unincorporated Areas) 2018 Q2",
        "Alameda County (Unincorporated Areas) 2018 Q3",
        "Alameda County (Unincorporated Areas) 2018 Q4",
    ]
    creates = plan_record_creates(plan, CONFIG)
    assert creates[1][SF["jurisdiction"]] == ["recJ2"]
    assert creates[1][SF["permits_total"]] == 0
    assert SF["data_flags"] not in creates[1]
    update = plan_record_updates(plan, CONFIG)[0]
    assert update.record_id == "recB0"
    assert set(update.expected_fields.values()) == {None}
    assert update.desired_fields[SF["quarter_start"]] == "2018-01-01"


def test_synced_table_needs_no_writes() -> None:
    plan = _plan(_synced())
    assert plan["matched_count"] == 8
    assert plan["unchanged_count"] == 8
    assert plan["change_count"] == 0
    assert plan["orphans"] == []
    require_eligible(plan)
    require_complete(plan)


def test_changed_quarter_rewrites_only_its_changed_fields() -> None:
    series = _series(
        {("SAN JOSE", 2): {"permits_ami": 5, "permits_total": 5, "data_flags": "x"}}
    )
    records = _synced()
    records[0][1][SF["data_flags"]] = "stale"
    plan = _plan(records, series)
    assert plan["update_count"] == 2
    assert plan["create_count"] == 0
    updates = {update["key"]: update for update in plan["updates"]}
    assert updates["San Jose 2018 Q2"]["before"] == {
        "data_flags": None,
        "permits_ami": 0,
        "permits_total": 0,
    }
    assert updates["San Jose 2018 Q2"]["after"] == {
        "data_flags": "x",
        "permits_ami": 5,
        "permits_total": 5,
    }
    assert updates["San Jose 2018 Q1"]["after"] == {"data_flags": None}
    with pytest.raises(AirtableSyncCoverageError):
        require_complete(plan)
    summary = summarize_plan(plan)
    assert summary["updated_field_counts"] == {
        "data_flags": 2,
        "permits_ami": 1,
        "permits_total": 1,
    }
    assert summary["updated_year_counts"] == {"2018": 2}
    assert not re.search(r"rec[A-Z]", json.dumps(summary))


def test_records_outside_the_series_are_orphans_not_writes() -> None:
    stray = _airtable_fields(_row("San Jose", "SAN JOSE", 1))
    stray[SF["year"]] = 2017
    stray[SF["key"]] = "San Jose 2017 Q1"
    plan = _plan([*_synced(), ("recOld", stray), ("recB0", {})])
    assert plan["apply_eligible"] is True
    assert plan["change_count"] == 0
    assert plan["unused_blank_count"] == 1
    assert plan["orphans"] == [
        {
            "key": "San Jose 2017 Q1",
            "reason": "jurisdiction, year, and quarter are outside the series",
        }
    ]


def test_unidentified_records_block_the_plan() -> None:
    unlinked = {SF["key"]: "stray", SF["year"]: 2018, SF["quarter"]: 1}
    double = _airtable_fields(_row("San Jose", "SAN JOSE", 1))
    double[SF["jurisdiction"]] = ["recJ1", "recJ2"]
    fractional = _airtable_fields(_row("San Jose", "SAN JOSE", 1))
    fractional[SF["year"]] = 2018.5
    del fractional[SF["key"]]
    plan = _plan(
        [*_synced(), ("recU1", unlinked), ("recU2", double), ("recU3", fractional)]
    )
    assert plan["apply_eligible"] is False
    [blocker] = plan["blockers"]
    assert blocker.endswith("'San Jose 2018 Q1', 'stray', with no key")
    with pytest.raises(AirtableSyncBlockedError, match="blocked"):
        require_eligible(plan)


def test_repeated_identity_blocks_and_is_not_written() -> None:
    records = _synced()
    duplicate = copy.deepcopy(records[0][1])
    duplicate[SF["permits_total"]] = 9
    plan = _plan([*records, ("recDup", duplicate)])
    assert plan["apply_eligible"] is False
    assert plan["blockers"] == [
        "APR Quarterly repeats a jurisdiction, year, and quarter: 'San Jose 2018 Q1'"
    ]
    assert plan["matched_count"] == 7
    assert plan["change_count"] == 0


def test_jurisdiction_mapping_problems_block() -> None:
    without_exclusion = JURISDICTION_RECORDS[:2]
    plan = _plan(_synced(), jurisdictions=without_exclusion)
    assert plan["blockers"] == ["Configured Airtable exclusions are absent"]
    extra = [*JURISDICTION_RECORDS, ("recJ9", {JF["name"]: "Brisbane"})]
    assert _plan(_synced(), jurisdictions=extra)["blockers"] == [
        "Airtable has jurisdictions outside the series mapping: BRISBANE"
    ]
    missing = [JURISDICTION_RECORDS[0], JURISDICTION_RECORDS[2]]
    plan = _plan([], jurisdictions=missing)
    assert plan["blockers"] == [
        "No single Airtable jurisdiction matches 'ALAMEDA COUNTY'"
    ]
    assert plan["create_count"] == 4
    duplicate = [*JURISDICTION_RECORDS, ("recJ6", {JF["name"]: "San  Jose"})]
    assert _plan(_synced(), jurisdictions=duplicate)["blockers"] == [
        "Airtable has duplicate canonical jurisdiction identities",
        "No single Airtable jurisdiction matches 'SAN JOSE'",
    ]


def test_series_build_blockers_block_publication() -> None:
    series = _series(blockers=("Permit quarters do not reconcile for Davis",))
    plan = _plan(_synced(series), series)
    assert plan["blockers"] == [
        "Series build: Permit quarters do not reconcile for Davis"
    ]
    assert plan["apply_eligible"] is False


def _mutated(mutate) -> dict[str, object]:
    series = _series()
    mutate(series)
    return series


def _set(position: int, column: str, value: object):
    def mutate(series: dict) -> None:
        series["rows"][position][SERIES_COLUMNS.index(column)] = value

    return mutate


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda s: s["rows"].pop(), "lacks quarters for ALAMEDA COUNTY"),
        (lambda s: s["rows"].append(list(s["rows"][0])), "repeats San Jose 2018 Q1"),
        (lambda s: s["columns"].reverse(), "columns changed"),
        (lambda s: s["metadata"].update(window_end="2019-12-31"), "window changed"),
        (lambda s: s["metadata"].pop("sync_blockers"), "lacks sync_blockers"),
        (lambda s: s["metadata"].update(row_count=9), "do not reconcile"),
        (lambda s: s["metadata"].update(cutoff_year=True), "cutoff year"),
        (_set(0, "key", "San Jose 2018 Q2"), "key is inconsistent"),
        (_set(0, "quarter_start", "2018-02-01"), "quarter_start"),
        (_set(0, "quarter", 5), "outside the series window"),
        (_set(0, "permits_total", -1), "nonnegative integer"),
        (_set(0, "permits_total", True), "nonnegative integer"),
        (_set(0, "data_flags", None), "data_flags must be a string"),
        (_set(0, "jurisdiction", " San Jose"), "trimmed string"),
    ],
)
def test_validate_series_rejects_gaps_and_inconsistencies(mutate, message) -> None:
    with pytest.raises(AirtableSyncError, match=message):
        validate_series(_mutated(mutate), expected_jurisdiction_count=2)


def test_validate_series_rejects_renames_and_wrong_jurisdiction_count() -> None:
    def rename(series: dict) -> None:
        for position in (1, 2, 3):
            row = series["rows"][position]
            row[SERIES_COLUMNS.index("jurisdiction")] = "San Jose City"
            row[SERIES_COLUMNS.index("key")] = f"San Jose City 2018 Q{position + 1}"

    with pytest.raises(AirtableSyncError, match="renames SAN JOSE"):
        validate_series(_mutated(rename), expected_jurisdiction_count=2)
    with pytest.raises(AirtableSyncError, match="expected 3"):
        validate_series(_series(), expected_jurisdiction_count=3)


def test_snapshot_rejects_values_and_types_airtable_should_not_hold() -> None:
    records = _synced()
    records[0][1][SF["year"]] = "2018"
    with pytest.raises(AirtableSyncSnapshotError, match="year is not a number"):
        _snapshot(records)
    records = _synced()
    records[0][1][SF["key"]] = 7
    with pytest.raises(AirtableSyncSnapshotError, match="key is not text"):
        _snapshot(records)
    types = {**SERIES_FIELD_TYPES, "permits_total": "singleLineText"}
    with pytest.raises(AirtableSyncSnapshotError, match="pinned type"):
        _snapshot(_synced(), series_types=types)


def test_snapshot_for_another_table_is_rejected() -> None:
    snapshot = {**_snapshot(_synced()), "series_table_id": "tblOther"}
    with pytest.raises(AirtableSyncSnapshotError, match="another table"):
        build_plan(series=_series(), snapshot=snapshot, config=CONFIG)


def test_state_change_between_plan_and_apply_is_rejected() -> None:
    plan = _plan([("recB0", {})])
    verify_plan_unchanged(plan, _plan([("recB0", {})]))
    with pytest.raises(AirtableSyncStateChangedError):
        verify_plan_unchanged(plan, _plan([("recB1", {})]))


@pytest.mark.parametrize(
    "change",
    [
        {"extra": True},
        {"config_version": True},
        {"series_table_id": CONFIG["jurisdictions_table_id"]},
        {"excluded_jurisdictions": ["Sitekick City", "Sitekick City "]},
        {"expected_jurisdiction_count": 0},
        {"series_fields": {**SF, "permits_total": SF["permits_ami"]}},
    ],
)
def test_config_rejects_unsafe_mappings(change: dict) -> None:
    with pytest.raises(AirtableSyncConfigurationError):
        normalize_config({**CONFIG, **change})
