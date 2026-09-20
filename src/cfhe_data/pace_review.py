"""Prepare an isolated, offline review of the Airtable RHNA pace calculation.

visibility: public

By default permit counts are held constant. An explicit baseline enables review
of source changes alongside the pace correction. Airtable record IDs and
before-values belong in an ignored, private artifact. No network or apply path.
"""

from __future__ import annotations

import datetime as dt
import math
from collections import Counter
from collections.abc import Mapping

from .airtable_sync import _airtable_identity, _target_bands, _validate_source
from .progress import calculate_progress
from .webflow import canonical_sha256

BASE_ID = "appjwA9nMuNRewKNo"
JURISDICTIONS = "tblMHCufKHzpCWPFJ"
RHNA = "tblOiiBUHLapyf25e"
NAME = "fldw8G1J4UIAvd7Yv"
UNINCORPORATED = "fldgi3VnJh5F5sIOK"
CITY_LINK = "fld76ruskUKCuiEtW"
CYCLE = "fldOwmEQHcSbngYA0"
CURRENT = "fld1h7uuhM0O5S5ue"
CORRECT_LINK = "fld9D4jFSeZONagWN"
TOTAL = "fldvW5QO6skRvpL0Q"
ALLOCATION = "fldNtc1LNYxV8HHBm"
PROGRESS_OVERRIDE = "fld9HQz1i9DipXRht"
TIME_REMAINING = "fldRpysucXnIQhz36"
STATUS = "fldz32T24Prq6BwXk"
PERMITS = {
    "vli": "fldJJnj92GzuQgwDu",
    "li": "fldVzjVptUkD7cwsz",
    "mi": "fldyXvK2Vy49fkKye",
    "ami": "fld7t1u9DMWRhRmqa",
}
ALLOCATIONS = {
    "vli": "fldFxNucslp94SBYq",
    "li": "fldtLexU98xpd9kc5",
    "mi": "fldv65hKdBuwKJrAH",
    "ami": "fldTzE6qJ6LEi3Mun",
}
METADATA_FIELDS = {
    "data_through": "APR Data Through",
    "period_start": "APR Planning Period Start",
    "period_end": "APR Planning Period End",
}


def _count(value: object, label: str, *, missing: bool = False) -> int | None:
    if value is None and missing:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
        or int(value) != value
    ):
        raise ValueError(f"{label} must be a known nonnegative integer")
    return int(value)


def _records(snapshot: Mapping[str, object], table_id: str) -> list[dict]:
    records = snapshot.get("records_by_table", {}).get(table_id)
    if not isinstance(records, list):
        raise ValueError(f"Snapshot is missing table {table_id}")  # noqa: TRY004
    ids = [record.get("id") for record in records]
    if any(not isinstance(value, str) for value in ids) or len(ids) != len(set(ids)):
        raise ValueError(f"Snapshot has invalid or duplicate records in {table_id}")
    if any(not isinstance(row.get("fields"), Mapping) for row in records):
        raise ValueError(f"Snapshot has malformed fields in {table_id}")
    return records


def _schema_fields(schema: Mapping[str, object], table: str) -> dict[str, dict]:
    matches = [item for item in schema.get("tables", []) if item.get("id") == table]
    if len(matches) != 1:
        raise ValueError(f"Schema does not identify exactly one {table} table")
    result = {field["id"]: field for field in matches[0]["fields"]}
    if len(result) != len(matches[0]["fields"]):
        raise ValueError("Schema contains duplicate field IDs")
    return result


def _formula_plan(fields: Mapping[str, dict]) -> dict[str, object]:
    for field_id in (TIME_REMAINING, STATUS):
        if fields.get(field_id, {}).get("type") != "formula" or not fields[
            field_id
        ].get("options", {}).get("isValid"):
            raise ValueError(f"Required formula {field_id} is missing or invalid")
    resolved = {}
    for logical, name in METADATA_FIELDS.items():
        matches = [field for field in fields.values() if field["name"] == name]
        if len(matches) > 1 or matches and matches[0]["type"] != "date":
            raise ValueError(f"APR metadata {name!r} must identify one date field")
        if matches:
            resolved[logical] = matches[0]["id"]
    through, start, end = (
        "{" + resolved.get(logical, METADATA_FIELDS[logical]) + "}"
        for logical in ("data_through", "period_start", "period_end")
    )
    duration = f"DATETIME_DIFF({end},{start},'days')"
    elapsed = f"DATETIME_DIFF({through},{start},'days')"
    valid = f"AND({through},{start},{end},{duration}>0)"
    remaining = f"IF({valid},1-MIN(1,MAX(0,{elapsed}/{duration})),BLANK())"
    # Multiplying integer counts by elapsed days avoids rounding the benchmark.
    pace = (
        f"IF({{{TOTAL}}}*{duration}>={{{ALLOCATION}}}*{elapsed},"
        '"On Target","Off Target")'
    )
    status = (
        f"IF(AND({valid},{{{ALLOCATION}}}>0,{elapsed}>0),"
        f"IF({elapsed}>={duration},IF({{{TOTAL}}}>={{{ALLOCATION}}},"
        f'"Target Met","Missed"),{pace}),BLANK())'
    )
    updates = []
    for field_id, current_formula in ((TIME_REMAINING, remaining), (STATUS, status)):
        old = fields[field_id]["options"]["formula"]
        updates.append(
            {
                "table_id": RHNA,
                "field_id": field_id,
                "name": fields[field_id]["name"],
                "expected_formula": old,
                "proposed_formula": f"IF({{{CURRENT}}}=1,{current_formula},{old})",
            }
        )
    return {
        "new_fields": [
            {
                "table_id": RHNA,
                "name": name,
                "type": "date",
                "options": {"dateFormat": {"name": "iso", "format": "YYYY-MM-DD"}},
            }
            for logical, name in METADATA_FIELDS.items()
            if logical not in resolved
        ],
        "resolved_metadata_field_ids": resolved,
        "formula_updates": updates,
        "historical_rows": "Existing formula expressions retained for noncurrent rows",
        "date_ownership": "Existing HE and RHNA dates remain unchanged",
        "binding": "Resolve newly created metadata field IDs before any record writes",
    }


def build_pace_review(
    *,
    totals: Mapping[str, object],
    cycle_policy: Mapping[str, object],
    schema: Mapping[str, object],
    snapshot: Mapping[str, object],
    baseline_totals: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Return a complete, private pace review and proposed metadata writes.

    ``snapshot`` is a field-ID export with ``records_by_table``, ``observed_at``,
    ``base_id``, ``complete``, ``terminal_offset_reached`` and ``rhna_scope``.
    It must contain every jurisdiction (plus Sitekick City) and exactly the
    current RHNA rows. Missing allocations remain unknown and block readiness.
    ``schema`` is the corresponding Airtable tables schema. ``baseline_totals``
    optionally identifies the complete source artifact matching live counts;
    then ``totals`` supplies reviewed new counts and planning dates. Both must
    cover the same jurisdictions, cycles and reporting cutoff. No writes occur.
    """
    metadata, rows = _validate_source(totals)
    expected_count = cycle_policy.get("expected_jurisdiction_count")
    if (
        type(expected_count) is not int
        or expected_count <= 0
        or len(rows) != expected_count
    ):
        raise ValueError(
            "Source does not contain the full expected jurisdiction coverage"
        )
    if metadata["provisional"]:
        raise ValueError("Pace review requires a complete reporting year")
    if metadata["cycle_policy_sha256"] != canonical_sha256(cycle_policy):
        raise ValueError("Source cycle policy digest does not match")
    if metadata["cycle_counts"] != cycle_policy.get("expected_cycle_counts"):
        raise ValueError("Source cycle counts do not match the policy")
    if baseline_totals is None:
        baseline_totals = totals
        baseline_metadata, baseline_rows = metadata, rows
    else:
        baseline_metadata, baseline_rows = _validate_source(baseline_totals)
        if (
            baseline_metadata["provisional"]
            or len(baseline_rows) != expected_count
            or baseline_metadata["cutoff_year"] != metadata["cutoff_year"]
            or baseline_metadata["cycle_counts"] != metadata["cycle_counts"]
        ):
            raise ValueError(
                "Baseline must preserve complete coverage, cycles and reporting cutoff"
            )
        old_cycles = {row["jurisdiction_key"]: row["cycle"] for row in baseline_rows}
        new_cycles = {row["jurisdiction_key"]: row["cycle"] for row in rows}
        if old_cycles != new_cycles:
            raise ValueError(
                "Baseline and proposed source must identify the same jurisdictions and cycles"
            )
    baseline_by_key = {row["jurisdiction_key"]: row for row in baseline_rows}
    if (
        snapshot.get("base_id") != BASE_ID
        or snapshot.get("complete") is not True
        or snapshot.get("terminal_offset_reached") is not True
        or snapshot.get("rhna_scope") != "current"
    ):
        raise ValueError("A complete current-cycle snapshot is required")
    observed = dt.datetime.fromisoformat(str(snapshot.get("observed_at", "")))
    if observed.tzinfo is None:
        raise ValueError("Snapshot observation time must include a timezone")
    fields = _schema_fields(schema, RHNA)
    required_types = {
        CITY_LINK: "multipleRecordLinks",
        CYCLE: "multipleLookupValues",
        CURRENT: "formula",
        CORRECT_LINK: "formula",
        TOTAL: "formula",
        ALLOCATION: "formula",
        PROGRESS_OVERRIDE: "number",
        **{value: "number" for value in (*PERMITS.values(), *ALLOCATIONS.values())},
    }
    for field_id, field_type in required_types.items():
        if fields.get(field_id, {}).get("type") != field_type:
            raise ValueError(f"Schema type changed for {field_id}")
    formula_plan = _formula_plan(fields)
    cities = _records(snapshot, JURISDICTIONS)
    rhna_records = _records(snapshot, RHNA)
    if len(cities) != expected_count + 1 or len(rhna_records) != expected_count:
        raise ValueError(
            "Airtable snapshot does not contain the full expected coverage"
        )
    names = {}
    excluded = 0
    for record in cities:
        f = record["fields"]
        if f.get(NAME) == "Sitekick City":
            excluded += 1
            continue
        checkbox = f.get(UNINCORPORATED, False)
        if not isinstance(checkbox, bool) or not isinstance(f.get(NAME), str):
            raise ValueError("Jurisdiction name or unincorporated status is invalid")  # noqa: TRY004
        identity = _airtable_identity(f[NAME], checkbox)
        if identity in names:
            raise ValueError("Airtable jurisdiction identities are duplicated")
        names[identity] = record["id"]
    if excluded != 1:
        raise ValueError("Expected exactly one excluded Sitekick City")
    current = {}
    for record in rhna_records:
        f = record["fields"]
        city_ids = f.get(CITY_LINK)
        if not isinstance(city_ids, list) or len(city_ids) != 1:
            raise ValueError(
                "Current RHNA row does not identify exactly one jurisdiction"
            )
        if city_ids[0] in current or f.get(CURRENT) != 1 or f.get(CORRECT_LINK) != 1:
            raise ValueError(
                "Current RHNA rows must be unique, current and correctly linked"
            )
        current[city_ids[0]] = record
    cutoff = dt.date(int(metadata["cutoff_year"]), 12, 31)
    comparisons, stage, unknowns = [], [], []
    for row in rows:
        key = row["jurisdiction_key"]
        identity = (key, key.endswith(" COUNTY"))
        city_id = names.get(identity)
        record = current.get(city_id)
        if not record:
            raise ValueError(f"Missing exact current Airtable target for {key}")
        f = record["fields"]
        if f.get(CYCLE) != [row["cycle"]]:
            raise ValueError(f"Current Airtable cycle disagrees for {key}")
        if f.get(PROGRESS_OVERRIDE, 0) not in (None, 0):
            raise ValueError(f"Progress override prevents a pace-only review for {key}")
        permits = _target_bands(row)
        before = baseline_by_key[key]
        before_permits = _target_bands(before)
        live = {
            band: _count(f.get(fid), f"{key}.{band}") for band, fid in PERMITS.items()
        }
        if (
            live != before_permits
            or _count(f.get(TOTAL), f"{key}.total") != before["total"]
        ):
            raise ValueError(f"Live permits disagree with the baseline source: {key}")
        allocations = {
            band: _count(f.get(fid), f"{key}.{band} allocation", missing=True)
            for band, fid in ALLOCATIONS.items()
        }
        allocation = _count(f.get(ALLOCATION), f"{key}.allocation", missing=True)
        if (
            allocation is not None
            and all(value is not None for value in allocations.values())
            and sum(allocations.values()) != allocation
        ):
            raise ValueError(f"Income-band allocations do not reconcile for {key}")
        start, end = (
            dt.date.fromisoformat(row[name]) for name in ("period_start", "period_end")
        )
        if end <= start:
            raise ValueError(f"Invalid source planning period for {key}")
        remaining = f.get(TIME_REMAINING)
        if (
            isinstance(remaining, bool)
            or not isinstance(remaining, (int, float))
            or not math.isfinite(remaining)
        ):
            raise ValueError(f"Missing live pace benchmark for {key}")
        pace = None
        status = None
        if allocation is None:
            unknowns.append({"jurisdiction_key": key, "field": "total_allocation"})
        else:
            pace = calculate_progress(row["total"], allocation, start, end, cutoff)
            if pace["on_pace"] is not None:
                status = (
                    ("Target Met" if pace["on_pace"] else "Missed")
                    if cutoff >= end
                    else ("On Target" if pace["on_pace"] else "Off Target")
                )
        by_income = {}
        for band, target in allocations.items():
            if target is None:
                unknowns.append(
                    {"jurisdiction_key": key, "field": f"{band}_allocation"}
                )
                by_income[band] = {"status": "unknown_allocation", "on_pace": None}
            else:
                by_income[band] = calculate_progress(
                    permits[band], target, start, end, cutoff
                )
        comparisons.append(
            {
                "jurisdiction_key": key,
                "jurisdiction": row["jurisdiction"],
                "cycle": row["cycle"],
                "data_status": row["data_status"],
                "before_data_status": before["data_status"],
                "permit_values": permits,
                "before_permit_values": before_permits,
                "after_permit_values": permits,
                "permit_value_difference": {
                    band: permits[band] - before_permits[band] for band in PERMITS
                },
                "total": row["total"],
                "before_total": before["total"],
                "after_total": row["total"],
                "count_change": row["total"] - before["total"],
                "counts_changed": permits != before_permits,
                "allocation_values": allocations,
                "total_allocation": allocation,
                "before_status": f.get(STATUS),
                "after_status": status,
                "status_changed": allocation is not None
                and status != (f.get(STATUS) or None),
                "before_elapsed_fraction": 1 - remaining,
                "before_expected_units": (1 - remaining) * allocation
                if allocation is not None
                else None,
                "planning_start": start.isoformat(),
                "planning_end": end.isoformat(),
                "before_planning_start": before["period_start"],
                "before_planning_end": before["period_end"],
                "pace": pace or {"status": "unknown_allocation", "on_pace": None},
                "pace_by_income": by_income,
            }
        )
        stage.append(
            {
                "table_id": RHNA,
                "record_id": record["id"],
                "jurisdiction_key": key,
                "expected_existing_fields": {
                    field: f.get(field)
                    for field in (
                        CITY_LINK,
                        CYCLE,
                        CURRENT,
                        CORRECT_LINK,
                        TOTAL,
                        ALLOCATION,
                        PROGRESS_OVERRIDE,
                        *PERMITS.values(),
                        *ALLOCATIONS.values(),
                        *formula_plan["resolved_metadata_field_ids"].values(),
                    )
                },
                "proposed_fields_by_name": {
                    METADATA_FIELDS["data_through"]: cutoff.isoformat(),
                    METADATA_FIELDS["period_start"]: start.isoformat(),
                    METADATA_FIELDS["period_end"]: end.isoformat(),
                },
                "proposed_permit_values_by_field_id": {
                    PERMITS[band]: permits[band]
                    for band in PERMITS
                    if permits[band] != before_permits[band]
                },
            }
        )
    return {
        "report_version": 1,
        "visibility": "non-public:private",
        "purpose": "Offline source and pace review; no publication"
        if any(row["counts_changed"] for row in comparisons)
        else "Offline pace-only review; no permit-count changes or publication",
        "source_totals_sha256": canonical_sha256(totals),
        "baseline_totals_sha256": canonical_sha256(baseline_totals),
        "source_metadata": metadata,
        "baseline_metadata": baseline_metadata,
        "cycle_policy_sha256": canonical_sha256(cycle_policy),
        "schema_sha256": canonical_sha256(schema),
        "snapshot_sha256": canonical_sha256(snapshot),
        "snapshot_observed_at": snapshot["observed_at"],
        "snapshot_component_observed_at": snapshot.get("component_observed_at", {}),
        "base_id": BASE_ID,
        "data_through": cutoff.isoformat(),
        "expected_jurisdiction_count": expected_count,
        "compared_jurisdiction_count": len(comparisons),
        "count_changes": sum(row["counts_changed"] for row in comparisons),
        "total_count_change": sum(row["count_change"] for row in comparisons),
        "coverage_complete": True,
        "unknown_allocations": unknowns,
        "ready_for_schema_migration_review": not unknowns,
        "status_change_count": sum(row["status_changed"] for row in comparisons),
        "before_status_counts": dict(
            Counter(row["before_status"] for row in comparisons)
        ),
        "after_status_counts": dict(
            Counter(
                row["after_status"]
                or (
                    "Unknown" if row["total_allocation"] is None else "No pace judgment"
                )
                for row in comparisons
            )
        ),
        "comparisons": comparisons,
        "proposed_metadata_updates": stage,
        "schema_migration": formula_plan,
        "publication_prerequisites": [
            "Fresh expected-value checks of source records and formula expressions",
            "Verified schema-write capability and resolved metadata field IDs",
            "Publisher support to refresh APR dates together with permit counts",
            "Readback of all metadata, pace formulas and public tracker statuses",
        ],
    }
