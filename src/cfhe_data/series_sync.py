# visibility: public
"""Guarded Airtable synchronization for the "APR Quarterly" series table.

``airtable_series.json`` is the only source for the table. Each Airtable record
is identified by its jurisdiction link, year, and quarter. The sync rewrites
changed fields on matched records, fills records that are entirely blank, and
creates the series rows that are still missing. It never deletes: a record
with a valid identity outside the series is reported as an orphan and left
alone, and a record that cannot be identified safely blocks the whole plan.
"""

from __future__ import annotations

import datetime as dt
import math
from collections import defaultdict
from collections.abc import Mapping, Sequence

from .airtable_api import AirtableClient, RecordUpdate
from .airtable_sync import (
    JURISDICTION_FIELD_TYPES,
    AirtableSyncBlockedError,
    AirtableSyncConfigurationError,
    AirtableSyncCoverageError,
    AirtableSyncError,
    AirtableSyncSnapshotError,
    AirtableSyncStateChangedError,
    _airtable_identity,
    _complete_export,
    _digest_mapping,
    _field_mapping,
    _links,
    _optional_date,
    _source_identity,
    _strict_keys,
    _string,
    _verify_schema,
)
from .series import SERIES_COLUMNS, SERIES_START
from .webflow import canonical_sha256

SERIES_FIELD_NAMES = tuple(
    name for name in SERIES_COLUMNS if name != "jurisdiction_key"
)
_TEXT_FIELDS = frozenset({"key", "data_flags"})
SERIES_FIELD_TYPES = {
    name: (
        "singleLineText"
        if name in _TEXT_FIELDS
        else "multipleRecordLinks"
        if name == "jurisdiction"
        else "date"
        if name == "quarter_start"
        else "number"
    )
    for name in SERIES_FIELD_NAMES
}
_COUNT_FIELDS = tuple(
    name for name in SERIES_COLUMNS if SERIES_FIELD_TYPES.get(name) == "number"
)
CONFIG_FIELDS = {
    "config_version",
    "base_id",
    "jurisdictions_table_id",
    "series_table_id",
    "excluded_jurisdictions",
    "expected_jurisdiction_count",
    "jurisdiction_fields",
    "jurisdiction_field_options_sha256",
    "series_fields",
    "series_field_options_sha256",
}
SERIES_METADATA_IDENTITY = (
    "cutoff_year",
    "last_updated",
    "source_manifest_sha256",
    "row_count",
    "jurisdiction_count",
)
# Airtable allows five requests per second per base and answers a burst with a
# 30 second penalty. The two clients run one after the other, so each paces a
# little below the limit to keep the handoff between them under it too.
REQUEST_INTERVAL_SECONDS = 0.25


def normalize_config(config: Mapping[str, object]) -> dict[str, object]:
    """Validate and canonicalize the public, nonsecret series mapping."""

    if not isinstance(config, Mapping):
        raise AirtableSyncConfigurationError("Series sync config must be an object")
    raw = dict(config)
    _strict_keys(raw, CONFIG_FIELDS, "series sync config")
    version = raw["config_version"]
    if isinstance(version, bool) or version != 1:
        raise AirtableSyncConfigurationError("Unsupported series sync config")
    ids = {
        "base_id": ("app", _string(raw["base_id"], "base_id")),
        "jurisdictions_table_id": (
            "tbl",
            _string(raw["jurisdictions_table_id"], "jurisdictions_table_id"),
        ),
        "series_table_id": ("tbl", _string(raw["series_table_id"], "series_table_id")),
    }
    for name, (prefix, value) in ids.items():
        if not value.startswith(prefix):
            raise AirtableSyncConfigurationError(f"{name} is not an Airtable ID")
    if ids["jurisdictions_table_id"][1] == ids["series_table_id"][1]:
        raise AirtableSyncConfigurationError("Series and jurisdiction tables differ")
    excluded = raw["excluded_jurisdictions"]
    if (
        not isinstance(excluded, list)
        or any(not isinstance(name, str) or not name.strip() for name in excluded)
        or len({name.strip() for name in excluded}) != len(excluded)
    ):
        raise AirtableSyncConfigurationError(
            "excluded_jurisdictions must list unique jurisdiction names"
        )
    count = raw["expected_jurisdiction_count"]
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise AirtableSyncConfigurationError(
            "expected_jurisdiction_count must be a positive integer"
        )
    return {
        "config_version": 1,
        **{name: value for name, (_prefix, value) in ids.items()},
        "excluded_jurisdictions": sorted(name.strip() for name in excluded),
        "expected_jurisdiction_count": count,
        "jurisdiction_fields": _field_mapping(
            raw["jurisdiction_fields"],
            set(JURISDICTION_FIELD_TYPES),
            "jurisdiction_fields",
        ),
        "jurisdiction_field_options_sha256": _digest_mapping(
            raw["jurisdiction_field_options_sha256"],
            set(JURISDICTION_FIELD_TYPES),
            "jurisdiction_field_options_sha256",
        ),
        "series_fields": _field_mapping(
            raw["series_fields"], set(SERIES_FIELD_NAMES), "series_fields"
        ),
        "series_field_options_sha256": _digest_mapping(
            raw["series_field_options_sha256"],
            set(SERIES_FIELD_NAMES),
            "series_field_options_sha256",
        ),
    }


# --------------------------------------------------------------------- source


def _source_count(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AirtableSyncError(f"{label} must be a nonnegative integer")
    return value


def validate_series(
    series: Mapping[str, object], *, expected_jurisdiction_count: int
) -> tuple[dict[str, object], list[dict[str, object]]]:
    """Check the series artifact and return its metadata and rows as objects.

    Every jurisdiction must carry one row for each quarter from 2018 through
    the cutoff year, under one display name, so the Airtable key and identity
    are unique and a missing quarter is never mistaken for a zero.
    """

    if not isinstance(series, Mapping) or set(series) != {
        "columns",
        "metadata",
        "rows",
    }:
        raise AirtableSyncError("Quarterly series artifact is malformed")
    if series["columns"] != list(SERIES_COLUMNS):
        raise AirtableSyncError("Quarterly series columns changed")
    metadata_raw = series["metadata"]
    rows_raw = series["rows"]
    if not isinstance(metadata_raw, Mapping) or not isinstance(rows_raw, list):
        raise AirtableSyncError("Quarterly series artifact is malformed")
    metadata = dict(metadata_raw)
    missing = sorted(
        {*SERIES_METADATA_IDENTITY, "quarters_per_jurisdiction", "sync_blockers"}
        - set(metadata)
    )
    if missing:
        raise AirtableSyncError("Quarterly series metadata lacks " + ", ".join(missing))
    cutoff_year = metadata["cutoff_year"]
    if (
        isinstance(cutoff_year, bool)
        or not isinstance(cutoff_year, int)
        or cutoff_year < SERIES_START.year
    ):
        raise AirtableSyncError("Quarterly series cutoff year is invalid")
    if (
        metadata.get("window_start") != SERIES_START.isoformat()
        or metadata.get("window_end") != dt.date(cutoff_year, 12, 31).isoformat()
    ):
        raise AirtableSyncError("Quarterly series window changed")
    blockers = metadata["sync_blockers"]
    if not isinstance(blockers, list) or any(
        not isinstance(item, str) or not item for item in blockers
    ):
        raise AirtableSyncError("Quarterly series sync_blockers must be strings")

    quarters = {
        (year, quarter)
        for year in range(SERIES_START.year, cutoff_year + 1)
        for quarter in range(1, 5)
    }
    rows: list[dict[str, object]] = []
    quarters_by_key: defaultdict[str, set[tuple[int, int]]] = defaultdict(set)
    display_by_key: dict[str, str] = {}
    key_by_display: dict[str, str] = {}
    for position, raw in enumerate(rows_raw):
        label = f"Quarterly series row {position}"
        if not isinstance(raw, list) or len(raw) != len(SERIES_COLUMNS):
            raise AirtableSyncError(f"{label} has the wrong shape")
        row = dict(zip(SERIES_COLUMNS, raw, strict=True))
        for name in ("key", "jurisdiction", "jurisdiction_key"):
            value = row[name]
            if not isinstance(value, str) or not value or value != value.strip():
                raise AirtableSyncError(f"{label} {name} must be a trimmed string")
        if not isinstance(row["data_flags"], str):
            raise AirtableSyncError(f"{label} data_flags must be a string")
        for name in _COUNT_FIELDS:
            _source_count(row[name], f"{label} {name}")
        year = row["year"]
        quarter = row["quarter"]
        if (year, quarter) not in quarters:
            raise AirtableSyncError(f"{label} quarter is outside the series window")
        assert isinstance(year, int) and isinstance(quarter, int)
        if row["quarter_start"] != dt.date(year, 3 * quarter - 2, 1).isoformat():
            raise AirtableSyncError(f"{label} quarter_start is inconsistent")
        jurisdiction = str(row["jurisdiction"])
        key = str(row["jurisdiction_key"])
        if row["key"] != f"{jurisdiction} {year} Q{quarter}":
            raise AirtableSyncError(f"{label} key is inconsistent")
        if display_by_key.setdefault(key, jurisdiction) != jurisdiction:
            raise AirtableSyncError(f"Quarterly series renames {key} between rows")
        if key_by_display.setdefault(jurisdiction, key) != key:
            raise AirtableSyncError(
                f"Quarterly series gives {jurisdiction!r} two jurisdiction keys"
            )
        if (year, quarter) in quarters_by_key[key]:
            raise AirtableSyncError(f"Quarterly series repeats {row['key']}")
        quarters_by_key[key].add((year, quarter))
        rows.append(row)

    incomplete = sorted(
        key for key, seen in quarters_by_key.items() if seen != quarters
    )
    if incomplete:
        raise AirtableSyncError(
            "Quarterly series lacks quarters for " + ", ".join(incomplete)
        )
    if (
        metadata["row_count"] != len(rows)
        or metadata["jurisdiction_count"] != len(quarters_by_key)
        or metadata["quarters_per_jurisdiction"] != len(quarters)
    ):
        raise AirtableSyncError("Quarterly series counts do not reconcile")
    if len(quarters_by_key) != expected_jurisdiction_count:
        raise AirtableSyncError(
            f"Quarterly series has {len(quarters_by_key)} jurisdictions, expected "
            f"{expected_jurisdiction_count}"
        )
    return metadata, rows


# ------------------------------------------------------------------- snapshot


def _clients(token: str, config: Mapping[str, object]) -> tuple[AirtableClient, ...]:
    jurisdiction_fields = config["jurisdiction_fields"]
    series_fields = config["series_fields"]
    assert isinstance(jurisdiction_fields, Mapping)
    assert isinstance(series_fields, Mapping)
    return (
        AirtableClient(
            token=token,
            base_id=str(config["base_id"]),
            table_id=str(config["jurisdictions_table_id"]),
            managed_field_ids=list(jurisdiction_fields.values()),
            min_request_interval_seconds=REQUEST_INTERVAL_SECONDS,
        ),
        AirtableClient(
            token=token,
            base_id=str(config["base_id"]),
            table_id=str(config["series_table_id"]),
            managed_field_ids=list(series_fields.values()),
            writable_field_ids=list(series_fields.values()),
            min_request_interval_seconds=REQUEST_INTERVAL_SECONDS,
        ),
    )


def fetch_snapshot(
    *, token: str, config: Mapping[str, object]
) -> tuple[dict[str, object], AirtableClient]:
    config = normalize_config(config)
    jurisdiction_client, series_client = _clients(token, config)
    snapshot = project_snapshot(
        config=config,
        jurisdiction_schema=jurisdiction_client.get_table_schema(),
        series_schema=series_client.get_table_schema(),
        jurisdiction_export=jurisdiction_client.list_records(verify_schema=False),
        series_export=series_client.list_records(verify_schema=False),
    )
    return snapshot, series_client


def _airtable_value(name: str, value: object, label: str) -> object:
    """Check a raw Airtable value against its field type and return it unchanged."""

    if value is None:
        return None
    field_type = SERIES_FIELD_TYPES[name]
    if field_type == "singleLineText":
        if not isinstance(value, str):
            raise AirtableSyncSnapshotError(f"{label} {name} is not text")
    elif field_type == "multipleRecordLinks":
        _links(value, f"{label} {name}")
    elif field_type == "date":
        _optional_date(value, f"{label} {name}")
    elif (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise AirtableSyncSnapshotError(f"{label} {name} is not a number")
    return value


def project_snapshot(
    *,
    config: Mapping[str, object],
    jurisdiction_schema: Mapping[str, object],
    series_schema: Mapping[str, object],
    jurisdiction_export: Mapping[str, object],
    series_export: Mapping[str, object],
) -> dict[str, object]:
    """Verify pinned field types and options, then project both tables."""

    config = normalize_config(config)
    jf = config["jurisdiction_fields"]
    sf = config["series_fields"]
    assert isinstance(jf, Mapping) and isinstance(sf, Mapping)
    _verify_schema(
        jurisdiction_schema,
        table_id=str(config["jurisdictions_table_id"]),
        field_ids=jf,
        expected_types=JURISDICTION_FIELD_TYPES,
        expected_options_sha256=config["jurisdiction_field_options_sha256"],
    )
    _verify_schema(
        series_schema,
        table_id=str(config["series_table_id"]),
        field_ids=sf,
        expected_types=SERIES_FIELD_TYPES,
        expected_options_sha256=config["series_field_options_sha256"],
    )
    jurisdictions = []
    for record in _complete_export(
        jurisdiction_export,
        base_id=str(config["base_id"]),
        table_id=str(config["jurisdictions_table_id"]),
    ):
        fields = record["fields"]
        name = fields.get(jf["name"])
        if not isinstance(name, str) or not name.strip():
            raise AirtableSyncSnapshotError("Airtable jurisdiction has no name")
        jurisdictions.append(
            {
                "record_id": record["record_id"],
                "name": name.strip(),
                "unincorporated": fields.get(jf["unincorporated"]) is True,
            }
        )
    records = []
    for record in _complete_export(
        series_export,
        base_id=str(config["base_id"]),
        table_id=str(config["series_table_id"]),
    ):
        fields = record["fields"]
        label = f"APR Quarterly record {record['record_id']}"
        records.append(
            {
                "record_id": record["record_id"],
                "fields": {
                    name: _airtable_value(name, fields.get(sf[name]), label)
                    for name in SERIES_FIELD_NAMES
                },
            }
        )
    return {
        "complete": True,
        "base_id": config["base_id"],
        "series_table_id": config["series_table_id"],
        "jurisdictions": jurisdictions,
        "records": records,
    }


# ----------------------------------------------------------------------- plan


def _empty(value: object) -> bool:
    return value is None or value == "" or value == []


def _same(current: object, desired: object) -> bool:
    if _empty(current) or _empty(desired):
        return _empty(current) and _empty(desired)
    return current == desired and isinstance(current, bool) == isinstance(desired, bool)


def _record_identity(fields: Mapping[str, object]) -> tuple[str, int, int] | None:
    links = fields["jurisdiction"]
    year = fields["year"]
    quarter = fields["quarter"]
    if (
        not isinstance(links, list)
        or len(links) != 1
        or not isinstance(year, (int, float))
        or not isinstance(quarter, (int, float))
        or year != int(year)
        or quarter not in (1, 2, 3, 4)
    ):
        return None
    return str(links[0]), int(year), int(quarter)


def _describe(record: Mapping[str, object]) -> str:
    key = record["fields"]["key"]
    return repr(key) if isinstance(key, str) and key.strip() else "with no key"


def build_plan(
    *,
    series: Mapping[str, object],
    snapshot: Mapping[str, object],
    config: Mapping[str, object],
) -> dict[str, object]:
    """Match every series row to one Airtable record and list exact writes.

    Series build blockers, coverage failures, duplicate identities, and records
    that are neither blank nor fully identified block the whole plan. Records
    with a valid identity outside the series are orphans: reported, not written.
    """

    config = normalize_config(config)
    metadata, rows = validate_series(
        series, expected_jurisdiction_count=int(config["expected_jurisdiction_count"])
    )
    if (
        snapshot.get("complete") is not True
        or snapshot.get("base_id") != config["base_id"]
        or snapshot.get("series_table_id") != config["series_table_id"]
    ):
        raise AirtableSyncSnapshotError(
            "Airtable series snapshot is incomplete or for another table"
        )
    blockers = [f"Series build: {item}" for item in metadata["sync_blockers"]]

    excluded = set(config["excluded_jurisdictions"])
    excluded_found: set[str] = set()
    by_identity: dict[tuple[str, bool], str] = {}
    duplicated: set[tuple[str, bool]] = set()
    for jurisdiction in snapshot["jurisdictions"]:
        name = jurisdiction["name"]
        if name in excluded:
            excluded_found.add(name)
            continue
        identity = _airtable_identity(name, jurisdiction["unincorporated"])
        if identity in by_identity:
            duplicated.add(identity)
            continue
        by_identity[identity] = jurisdiction["record_id"]
    if duplicated:
        blockers.append("Airtable has duplicate canonical jurisdiction identities")
    if excluded_found != excluded:
        blockers.append("Configured Airtable exclusions are absent")
    link_by_key: dict[str, str] = {}
    used: set[tuple[str, bool]] = set()
    for key in sorted({str(row["jurisdiction_key"]) for row in rows}):
        identity = _source_identity({"jurisdiction_key": key})
        target = by_identity.get(identity)
        if target is None or identity in duplicated:
            blockers.append(f"No single Airtable jurisdiction matches {key!r}")
            continue
        used.add(identity)
        link_by_key[key] = target
    outside = sorted(by_identity.keys() - used - duplicated)
    if outside:
        blockers.append(
            "Airtable has jurisdictions outside the series mapping: "
            + ", ".join(key for key, _unincorporated in outside)
        )

    desired: list[tuple[tuple[str, int, int], dict[str, object]]] = []
    for row in rows:
        link = link_by_key.get(str(row["jurisdiction_key"]))
        if link is None:
            continue
        fields = {name: row[name] for name in SERIES_FIELD_NAMES}
        fields["jurisdiction"] = [link]
        fields["data_flags"] = row["data_flags"] or None
        desired.append(((link, int(row["year"]), int(row["quarter"])), fields))
    desired_identities = {identity for identity, _fields in desired}

    by_record_identity: defaultdict[tuple[str, int, int], list[dict[str, object]]]
    by_record_identity = defaultdict(list)
    blanks: list[dict[str, object]] = []
    unidentified: list[str] = []
    for record in snapshot["records"]:
        fields = record["fields"]
        if all(_empty(value) for value in fields.values()):
            blanks.append(record)
            continue
        identity = _record_identity(fields)
        if identity is None:
            unidentified.append(_describe(record))
            continue
        by_record_identity[identity].append(record)
    if unidentified:
        blockers.append(
            "APR Quarterly records lack one jurisdiction link, a whole year, and "
            "a quarter from 1 to 4: " + ", ".join(sorted(unidentified))
        )
    repeated = sorted(
        _describe(records[0])
        for identity, records in by_record_identity.items()
        if identity in desired_identities and len(records) > 1
    )
    if repeated:
        blockers.append(
            "APR Quarterly repeats a jurisdiction, year, and quarter: "
            + ", ".join(repeated)
        )
    orphans = sorted(
        (
            {
                "key": record["fields"]["key"],
                "reason": "jurisdiction, year, and quarter are outside the series",
            }
            for identity, records in by_record_identity.items()
            if identity not in desired_identities
            for record in records
        ),
        key=lambda item: str(item["key"]),
    )

    updates: list[dict[str, object]] = []
    missing: list[dict[str, object]] = []
    unchanged_count = 0
    matched_count = 0
    for identity, fields in desired:
        records = by_record_identity.get(identity, [])
        if len(records) > 1:
            continue
        if not records:
            missing.append(fields)
            continue
        matched_count += 1
        current = records[0]["fields"]
        changed = {
            name: value
            for name, value in fields.items()
            if not _same(current[name], value)
        }
        if not changed:
            unchanged_count += 1
            continue
        updates.append(
            {
                "record_id": records[0]["record_id"],
                "key": fields["key"],
                "reused_blank": False,
                "before": {name: current[name] for name in changed},
                "after": changed,
            }
        )
    reused = list(zip(blanks, missing, strict=False))
    for record, fields in reused:
        after = {name: value for name, value in fields.items() if value is not None}
        updates.append(
            {
                "record_id": record["record_id"],
                "key": fields["key"],
                "reused_blank": True,
                "before": {name: record["fields"][name] for name in after},
                "after": after,
            }
        )
    creates = [
        {name: value for name, value in fields.items() if value is not None}
        for fields in missing[len(reused) :]
    ]

    source = {
        "series_sha256": canonical_sha256(dict(series)),
        **{name: metadata[name] for name in SERIES_METADATA_IDENTITY},
    }
    plan: dict[str, object] = {
        "plan_version": 1,
        "base_id": config["base_id"],
        "series_table_id": config["series_table_id"],
        "source": source,
        "airtable_record_count": len(snapshot["records"]),
        "desired_row_count": len(rows),
        "matched_count": matched_count,
        "unchanged_count": unchanged_count,
        "update_count": len(updates) - len(reused),
        "reused_blank_count": len(reused),
        "create_count": len(creates),
        "change_count": len(updates) + len(creates),
        "unused_blank_count": len(blanks) - len(reused),
        "orphan_count": len(orphans),
        "blockers": blockers,
        "apply_eligible": not blockers,
        "orphans": orphans,
        "updates": updates,
        "creates": creates,
    }
    plan["plan_sha256"] = canonical_sha256(
        {"source": source, "updates": updates, "creates": creates}
    )
    return plan


def plan_record_updates(
    plan: Mapping[str, object], config: Mapping[str, object]
) -> list[RecordUpdate]:
    sf = normalize_config(config)["series_fields"]
    assert isinstance(sf, Mapping)
    return [
        RecordUpdate(
            record_id=str(update["record_id"]),
            expected_fields={
                sf[name]: value for name, value in update["before"].items()
            },
            desired_fields={sf[name]: value for name, value in update["after"].items()},
        )
        for update in plan["updates"]
    ]


def plan_record_creates(
    plan: Mapping[str, object], config: Mapping[str, object]
) -> list[dict[str, object]]:
    sf = normalize_config(config)["series_fields"]
    assert isinstance(sf, Mapping)
    return [
        {sf[name]: value for name, value in fields.items()}
        for fields in plan["creates"]
    ]


def verify_plan_unchanged(
    plan: Mapping[str, object], fresh_plan: Mapping[str, object]
) -> None:
    if fresh_plan["plan_sha256"] != plan["plan_sha256"]:
        raise AirtableSyncStateChangedError(
            "Airtable or the series changed between planning and publication"
        )


def require_eligible(plan: Mapping[str, object]) -> None:
    if plan["apply_eligible"] is not True:
        raise AirtableSyncBlockedError(
            "Quarterly series publication is blocked: " + "; ".join(plan["blockers"])
        )


def require_complete(plan: Mapping[str, object]) -> None:
    if plan["change_count"] != 0:
        raise AirtableSyncCoverageError(
            "Airtable readback does not match the quarterly series"
        )


def summarize_plan(plan: Mapping[str, object]) -> dict[str, object]:
    """Counts, blockers, and orphan keys; never record IDs or field values."""

    keys = (
        "airtable_record_count",
        "desired_row_count",
        "matched_count",
        "unchanged_count",
        "update_count",
        "reused_blank_count",
        "create_count",
        "change_count",
        "unused_blank_count",
        "orphan_count",
        "apply_eligible",
        "blockers",
        "orphans",
        "plan_sha256",
    )
    summary = {key: plan[key] for key in keys}
    summary["source"] = dict(plan["source"])
    changed_fields: defaultdict[str, int] = defaultdict(int)
    updated_years: defaultdict[str, int] = defaultdict(int)
    for update in plan["updates"]:
        if update["reused_blank"]:
            continue
        for name in update["after"]:
            changed_fields[name] += 1
        updated_years[str(update["key"]).rsplit(" ", 2)[-2]] += 1
    summary["updated_field_counts"] = dict(sorted(changed_fields.items()))
    summary["updated_year_counts"] = dict(sorted(updated_years.items()))
    return summary


__all__: Sequence[str] = (
    "SERIES_FIELD_NAMES",
    "SERIES_FIELD_TYPES",
    "build_plan",
    "fetch_snapshot",
    "normalize_config",
    "plan_record_creates",
    "plan_record_updates",
    "project_snapshot",
    "require_complete",
    "require_eligible",
    "summarize_plan",
    "validate_series",
    "verify_plan_unchanged",
)
