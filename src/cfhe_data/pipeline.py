# visibility: public
"""Validated HCD ingestion, aggregation, and artifact generation."""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import os
import re
import tempfile
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path

from .dedupe import deduplicate_carried_forward
from .models import (
    DEFAULT_CATEGORY_FIELDS,
    ENTITLEMENT_CATEGORY_FIELDS,
    DedupAuditEntry,
    DedupResult,
    PermitRecord,
)
from .normalize import normalized_key
from .schema import (
    CsvContract,
    load_contract,
    open_validated_csv,
    parse_date,
    parse_nonnegative_integer,
    parse_planning_period,
    validate_json_document,
)

DEDUPE_PROFILE = "audited-apr-snapshots-v4"
CATEGORY_NAMES = (
    "acutely_low",
    "extremely_low",
    "very_low",
    "low",
    "moderate",
    "above_moderate",
)
CSV_COLUMNS = (
    "Jurisdiction",
    "Acutely Low",
    "Extremely Low",
    "Very Low",
    "Low",
    "Moderate",
    "Above Moderate",
    "Total",
    "Undated Permits",
    "Last Updated",
)
RHNA_NUMERIC_FIELDS = (
    "VLI UNITS",
    "RHNA VLI",
    "LI UNITS",
    "RHNA LI",
    "MOD UNITS",
    "RHNA MOD",
    "ABOVE MOD UNITS",
    "RHNA ABOVE MOD",
)

# HCD's live regional schedule, verified 2026-09-19, supersedes the dates
# still present in the sixth-cycle CSV for these jurisdictions only.
# https://www.hcd.ca.gov/housing-open-data-tools/housing-element-regional-housing-needs-determination-schedule
# Exact official rows and raw-response hash: config/siskiyou_planning_period_correction.json.
SISKIYOU_SIXTH_CYCLE_KEYS = frozenset(
    {
        "DORRIS",
        "DUNSMUIR",
        "ETNA",
        "FORT JONES",
        "MONTAGUE",
        "MOUNT SHASTA",
        "SISKIYOU COUNTY",
        "TULELAKE",
        "WEED",
        "YREKA",
    }
)
SISKIYOU_PREVIOUS_PERIOD = (dt.date(2022, 11, 15), dt.date(2030, 11, 15))
SISKIYOU_CURRENT_PERIOD = (dt.date(2023, 2, 15), dt.date(2031, 2, 15))


@dataclass(frozen=True, slots=True)
class ActivitySpec:
    """The Table A2 columns that date, total, and split one reported activity."""

    date_field: str
    total_field: str
    category_fields: tuple[tuple[str, ...], ...]
    # The date and count columns of another stage. In the dedupe pass that
    # recognizes rows carried forward from earlier reports, two rows that
    # repeat both also link without a parcel or address match.
    corroboration: tuple[str, str]

    @property
    def fields(self) -> tuple[str, ...]:
        return (
            self.date_field,
            self.total_field,
            *(field for group in self.category_fields for field in group),
        )


PERMIT_ACTIVITY = ActivitySpec(
    date_field="BP_ISSUE_DT1",
    total_field="NO_BUILDING_PERMITS",
    category_fields=DEFAULT_CATEGORY_FIELDS,
    corroboration=("ENT_APPROVE_DT1", "NO_ENTITLEMENTS"),
)
ENTITLEMENT_ACTIVITY = ActivitySpec(
    date_field="ENT_APPROVE_DT1",
    total_field="NO_ENTITLEMENTS",
    category_fields=ENTITLEMENT_CATEGORY_FIELDS,
    corroboration=(PERMIT_ACTIVITY.date_field, PERMIT_ACTIVITY.total_field),
)
# Completions (certificates of occupancy or other readiness) use Table A2's
# CO_ income columns, which reconcile to NO_OTHER_FORMS_OF_READINESS.
COMPLETION_ACTIVITY = ActivitySpec(
    date_field="CO_ISSUE_DT1",
    total_field="NO_OTHER_FORMS_OF_READINESS",
    category_fields=(
        ("CO_ACUTELY_LOW_INCOME_DR", "CO_ACUTELY_LOW_INCOME_NDR"),
        ("CO_EXTREMELY_LOW_INCOME_DR", "CO_EXTREMELY_LOW_INCOME_NDR"),
        ("CO_VLOW_INCOME_DR", "CO_VLOW_INCOME_NDR"),
        ("CO_LOW_INCOME_DR", "CO_LOW_INCOME_NDR"),
        ("CO_MOD_INCOME_DR", "CO_MOD_INCOME_NDR"),
        ("CO_ABOVE_MOD_INCOME",),
    ),
    corroboration=(PERMIT_ACTIVITY.date_field, PERMIT_ACTIVITY.total_field),
)


def deduplicate_activity(
    activity: ActivitySpec, records: Sequence[PermitRecord]
) -> DedupResult:
    """Deduplicate one activity's records under the carried forward profile."""

    return deduplicate_carried_forward(records, corroboration=activity.corroboration)


ENTITLEMENT_METADATA_FIELDS = (
    "entitlement_selected_row_count",
    "entitlement_selected_units",
    "entitlement_retained_row_count",
    "entitlement_removed_row_count",
    "entitlement_removed_units",
    "undated_entitlements",
)
ENTITLEMENT_ROW_FIELDS = (
    *(f"entitlement_{name}" for name in CATEGORY_NAMES),
    "entitlement_total",
    "undated_entitlements",
    "entitlement_data_status",
)


@dataclass(frozen=True, slots=True)
class PlanningPeriod:
    key: str
    display: str
    start: dt.date
    end: dt.date
    cycle_started: bool


@dataclass(frozen=True, slots=True)
class BuildArtifacts:
    jurisdiction_json: Path
    jurisdiction_csv: Path
    audit_summary: Path
    decision_ledger: Path
    review_ledger: Path
    selected_rows: int
    retained_rows: int
    removed_rows: int
    total_units: int


@dataclass(frozen=True, slots=True)
class AirtableBuildArtifacts:
    """Current-cycle totals and audit files intended for Airtable publication."""

    jurisdiction_json: Path
    audit_summary: Path
    decision_ledger: Path
    review_ledger: Path
    selected_rows: int
    retained_rows: int
    removed_rows: int
    total_units: int
    entitlement_audit_summary: Path
    entitlement_decision_ledger: Path
    entitlement_review_ledger: Path
    entitlement_selected_rows: int
    entitlement_retained_rows: int
    entitlement_removed_rows: int
    entitlement_total_units: int


def _display_jurisdiction(value: object) -> str:
    display = str(value or "").strip()
    display = re.sub(
        r"\s*\(\s*Unincorporated(?:\s+Areas?)?\s*\)\s*$",
        "",
        display,
        flags=re.IGNORECASE,
    ).strip()
    if display and display == display.upper():
        display = display.title()
    display = display.replace("Mcfarland", "McFarland")
    if normalized_key(display).endswith(" COUNTY"):
        display += " (Unincorporated)"
    return display


def load_planning_periods(
    path: Path, contract: CsvContract
) -> dict[str, PlanningPeriod]:
    periods: dict[str, PlanningPeriod] = {}
    with open_validated_csv(path, contract) as opened:
        for record_index, _physical_line, row in opened.rows():
            key = normalized_key(row["Jurisdiction"])
            if not key:
                raise ValueError(f"Blank jurisdiction at RHNA record {record_index}")
            if key in periods:
                raise ValueError(
                    f"Duplicate RHNA jurisdiction at record {record_index}: {key}"
                )
            start, end = parse_planning_period(
                row["Planning Period"], record=record_index
            )
            if key in SISKIYOU_SIXTH_CYCLE_KEYS:
                if (start, end) not in (
                    SISKIYOU_PREVIOUS_PERIOD,
                    SISKIYOU_CURRENT_PERIOD,
                ):
                    raise ValueError(
                        f"Unreviewed Siskiyou planning period at record {record_index}: "
                        f"{key}, {start} to {end}"
                    )
                start, end = SISKIYOU_CURRENT_PERIOD
            for field in RHNA_NUMERIC_FIELDS:
                parse_nonnegative_integer(row[field], field=field, record=record_index)
            started_text = row["6th Cycle Started"].strip().upper()
            if started_text not in {"TRUE", "FALSE"}:
                raise ValueError(
                    f"6th Cycle Started must be TRUE or FALSE at record {record_index}"
                )
            periods[key] = PlanningPeriod(
                key=key,
                display=_display_jurisdiction(row["Jurisdiction"]),
                start=start,
                end=end,
                cycle_started=started_text == "TRUE",
            )
    if not periods:
        raise ValueError("RHNA source contains no jurisdictions")
    return periods


def _strict_categories(
    row: Mapping[str, str],
    record_index: int,
    category_fields: tuple[tuple[str, ...], ...] = DEFAULT_CATEGORY_FIELDS,
) -> tuple[int, ...]:
    return tuple(
        sum(
            parse_nonnegative_integer(row[field], field=field, record=record_index)
            for field in group
        )
        for group in category_fields
    )


def _activity_record(
    activity: ActivitySpec,
    row: Mapping[str, str],
    *,
    record_index: int,
    physical_line: int,
    report_year: int,
    jurisdiction: str,
    period: PlanningPeriod,
    effective_end: dt.date,
    fingerprint_fields: Sequence[str],
) -> PermitRecord | None:
    """Return the row's record for one activity, or None when it is out of scope.

    The activity date occupies the record's ``permit_date`` slot, so snapshot
    deduplication blocks entitlement rows on their own approval date.
    """

    activity_date = parse_date(
        row[activity.date_field],
        field=activity.date_field,
        record=record_index,
    )
    if activity_date is None:
        included = period.start.year <= report_year <= effective_end.year
    else:
        included = period.start <= activity_date <= effective_end
    if not included:
        return None
    categories = _strict_categories(row, record_index, activity.category_fields)
    reported_total = parse_nonnegative_integer(
        row[activity.total_field],
        field=activity.total_field,
        record=record_index,
    )
    if not (sum(categories) or reported_total):
        return None
    return PermitRecord.from_values(
        source_record_index=record_index,
        csv_physical_line=physical_line,
        report_year=report_year,
        jurisdiction=jurisdiction,
        jurisdiction_display=period.display,
        permit_date=activity_date,
        tracking_id=row["JURS_TRACKING_ID"],
        project_name=row["PROJECT_NAME"],
        apn=row["APN"],
        street_address=row["STREET_ADDRESS"],
        standard_address=row["STD_ADDRESS"],
        latitude=row["LATITUDE"],
        longitude=row["LONGITUDE"],
        categories=categories,
        reported_total=reported_total,
        unit_category=row["UNIT_CAT"],
        tenure=row["TENURE"],
        raw_row=row,
        fingerprint_fields=fingerprint_fields,
    )


def select_activity_records(
    path: Path,
    contract: CsvContract,
    periods: Mapping[str, PlanningPeriod],
    cutoff_year: int,
    activities: Sequence[ActivitySpec],
    report_year_counts: Counter[tuple[str, int]] | None = None,
) -> tuple[tuple[tuple[PermitRecord, ...], dict[str, object]], ...]:
    """Select every activity's rows in one pass over Table A2.

    Each activity gets the same planning period window and its own records,
    statistics, and reconciliation check, in the order the activities are given.
    When ``report_year_counts`` is given, it also counts every raw row by
    jurisdiction key and report year, selected or not.
    """

    if cutoff_year < 2018:
        raise ValueError("cutoff_year must be 2018 or later")
    if not activities:
        raise ValueError("At least one Table A2 activity is required")
    cutoff_date = dt.date(cutoff_year, 12, 31)
    records: list[list[PermitRecord]] = [[] for _ in activities]
    selected_by_jurisdiction: list[Counter[str]] = [Counter() for _ in activities]
    mismatch_rows = [0] * len(activities)
    mismatch_units = [0] * len(activities)
    raw_jurisdictions: set[str] = set()
    raw_row_count = 0
    max_report_year = 0

    with open_validated_csv(path, contract) as opened:
        fingerprint_fields = opened.fieldnames
        missing = sorted(
            {field for activity in activities for field in activity.fields}
            - set(fingerprint_fields)
        )
        if missing:
            raise ValueError(
                f"{path} is missing Table A2 activity columns: {', '.join(missing)}"
            )
        for record_index, physical_line, row in opened.rows():
            raw_row_count += 1
            jurisdiction = normalized_key(row["JURIS_NAME"])
            raw_jurisdictions.add(jurisdiction)
            report_year = parse_nonnegative_integer(
                row["YEAR"], field="YEAR", record=record_index
            )
            if report_year == 0:
                raise ValueError(f"YEAR must be positive at record {record_index}")
            max_report_year = max(max_report_year, report_year)
            if report_year_counts is not None:
                report_year_counts[(jurisdiction, report_year)] += 1
            period = periods.get(jurisdiction)
            if period is None:
                continue
            effective_end = min(period.end, cutoff_date)
            for position, activity in enumerate(activities):
                record = _activity_record(
                    activity,
                    row,
                    record_index=record_index,
                    physical_line=physical_line,
                    report_year=report_year,
                    jurisdiction=jurisdiction,
                    period=period,
                    effective_end=effective_end,
                    fingerprint_fields=fingerprint_fields,
                )
                if record is None:
                    continue
                if record.units != record.reported_total:
                    mismatch_rows[position] += 1
                    mismatch_units[position] += record.units - record.reported_total
                records[position].append(record)
                selected_by_jurisdiction[position][jurisdiction] += 1

    selections: list[tuple[tuple[PermitRecord, ...], dict[str, object]]] = []
    for position, activity in enumerate(activities):
        stats = {
            "raw_row_count": raw_row_count,
            "raw_jurisdiction_count": len(raw_jurisdictions),
            "raw_jurisdictions_without_planning_period": sorted(
                raw_jurisdictions - set(periods)
            ),
            "planning_periods_without_selected_rows": sorted(
                key for key in periods if selected_by_jurisdiction[position][key] == 0
            ),
            "max_report_year": max_report_year,
            "selected_row_count": len(records[position]),
            "reported_total_mismatch_rows": mismatch_rows[position],
            "reported_total_net_difference": mismatch_units[position],
        }
        if mismatch_rows[position]:
            raise ValueError(
                f"{mismatch_rows[position]} selected Table A2 rows do not reconcile "
                f"with {activity.total_field}; net category difference "
                f"{mismatch_units[position]}"
            )
        selections.append((tuple(records[position]), stats))
    return tuple(selections)


def select_permit_records(
    path: Path,
    contract: CsvContract,
    periods: Mapping[str, PlanningPeriod],
    cutoff_year: int,
) -> tuple[tuple[PermitRecord, ...], dict[str, object]]:
    ((records, stats),) = select_activity_records(
        path, contract, periods, cutoff_year, (PERMIT_ACTIVITY,)
    )
    return records, stats


def _vector_sum(records: Sequence[PermitRecord]) -> tuple[int, ...]:
    totals = [0] * len(CATEGORY_NAMES)
    for record in records:
        for index, value in enumerate(record.categories):
            totals[index] += value
    return tuple(totals)


def _removal_rule_stats(result: DedupResult) -> dict[str, dict[str, object]]:
    audit_by_index = {entry.source_index: entry for entry in result.audit}
    rule_stats: dict[str, dict[str, object]] = {}
    for rule in sorted(result.counts_by_rule()):
        records = tuple(
            record
            for record in result.removed
            if audit_by_index[record.source_index].rule == rule
        )
        categories = _vector_sum(records)
        rule_stats[rule] = {
            "rows": len(records),
            "units": sum(categories),
            "categories": dict(zip(CATEGORY_NAMES, categories)),
        }
    return rule_stats


def _manifest_digest(manifest: Mapping[str, object]) -> str:
    payload = json.dumps(
        manifest,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _file_fingerprint(path: Path) -> tuple[int, str, str]:
    byte_count = 0
    sha256 = hashlib.sha256()
    md5 = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            byte_count += len(chunk)
            sha256.update(chunk)
            md5.update(chunk)
    return byte_count, sha256.hexdigest(), md5.hexdigest()


def _verify_manifest_source(
    *, label: str, path: Path, source_entry: Mapping[str, object]
) -> None:
    expected_bytes = source_entry.get("bytes")
    expected_sha256 = source_entry.get("sha256")
    if expected_bytes is None or not expected_sha256:
        raise ValueError(f"{label} manifest entry must include bytes and SHA-256")

    actual_bytes, actual_sha256, actual_md5 = _file_fingerprint(path)
    if actual_bytes != expected_bytes:
        raise ValueError(
            f"{label} byte count does not match the source manifest: "
            f"{actual_bytes} != {expected_bytes}"
        )
    if actual_sha256.casefold() != str(expected_sha256).casefold():
        raise ValueError(f"{label} SHA-256 does not match the source manifest")

    expected_md5 = source_entry.get("md5")
    if expected_md5 and actual_md5.casefold() != str(expected_md5).casefold():
        raise ValueError(f"{label} MD5 does not match the source manifest")


def _source_last_updated(manifest: Mapping[str, object]) -> str:
    sources = manifest.get("sources")
    if not isinstance(sources, Mapping):
        raise TypeError("Source manifest has no sources mapping")
    table = sources.get("table_a2")
    if not isinstance(table, Mapping):
        raise TypeError("Source manifest has no table_a2 entry")
    catalog_last_modified = table.get("catalog_last_modified")
    if catalog_last_modified:
        try:
            return (
                dt.datetime.fromisoformat(str(catalog_last_modified)).date().isoformat()
            )
        except (TypeError, ValueError, OverflowError):
            pass
    last_modified = table.get("last_modified")
    if last_modified:
        try:
            return parsedate_to_datetime(str(last_modified)).date().isoformat()
        except (TypeError, ValueError, OverflowError):
            pass
    retrieved_at = table.get("retrieved_at")
    if not retrieved_at:
        raise ValueError("table_a2 manifest entry has no usable update date")
    return dt.datetime.fromisoformat(str(retrieved_at)).date().isoformat()


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".part",
        delete=False,
    ) as handle:
        handle.write(text)
        temporary = Path(handle.name)
    os.replace(temporary, path)


def _json_text(value: object) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _decision_ledger_row(
    record: PermitRecord,
    audit: DedupAuditEntry | None,
    *,
    cycle: str | None = None,
    date_key: str = "permit_date",
) -> dict[str, object]:
    row: dict[str, object] = {
        "source_record_index": record.source_index,
        "csv_physical_line": record.csv_physical_line,
        "row_sha256": hashlib.sha256(
            json.dumps(
                record.raw_row,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest(),
        "jurisdiction": record.jurisdiction,
        "report_year": record.report_year,
        date_key: record.permit_date.isoformat() if record.permit_date else None,
        "categories": dict(zip(CATEGORY_NAMES, record.categories)),
        "units": record.units,
        "decision": "removed_earlier_snapshot" if audit else "retained",
    }
    if cycle is not None:
        row["cycle"] = cycle
    if audit is not None:
        row.update(
            {
                "rule": audit.rule,
                "matched_source_indices": list(audit.matched_source_indices),
                "retained_report_year": audit.retained_report_year,
                "retained_source_indices": list(audit.retained_source_indices),
                "lineage_resolution": (
                    "direct"
                    if audit.matched_source_indices == audit.retained_source_indices
                    else "transitive"
                ),
                "evidence": dict(audit.details),
            }
        )
    return row


def _write_decision_ledgers(
    *,
    selected: Sequence[PermitRecord],
    result: DedupResult,
    decision_ledger: Path,
    review_ledger: Path,
    cycles: Mapping[str, str] | None = None,
    date_key: str = "permit_date",
) -> None:
    """Write the complete ledger and a compact closed set of review evidence."""

    audit_by_index = {entry.source_index: entry for entry in result.audit}
    review_source_indices = set(audit_by_index)
    for audit in result.audit:
        review_source_indices.update(audit.matched_source_indices)
        review_source_indices.update(audit.retained_source_indices)
    ledger_lines: list[str] = []
    review_lines: list[str] = []
    for record in sorted(selected, key=lambda item: item.source_index):
        audit = audit_by_index.get(record.source_index)
        cycle = cycles[record.jurisdiction] if cycles is not None else None
        ledger_row = _decision_ledger_row(record, audit, cycle=cycle, date_key=date_key)
        rendered = json.dumps(
            ledger_row, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )
        ledger_lines.append(rendered)
        if record.source_index in review_source_indices:
            review_lines.append(rendered)
    _atomic_write(
        decision_ledger, "\n".join(ledger_lines) + ("\n" if ledger_lines else "")
    )
    _atomic_write(
        review_ledger, "\n".join(review_lines) + ("\n" if review_lines else "")
    )


def _load_airtable_cycle_policy(
    path: Path,
    sixth_cycle_periods: Mapping[str, PlanningPeriod],
) -> tuple[dict[str, PlanningPeriod], dict[str, str], str]:
    """Load an exact mixed-cycle policy and apply it to official sixth-cycle periods."""

    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise TypeError("Airtable cycle policy must be a JSON object")
    expected_root = {
        "policy_version",
        "default_cycle",
        "expected_jurisdiction_count",
        "expected_cycle_counts",
        "overrides",
        "source",
    }
    if set(raw) != expected_root:
        missing = sorted(expected_root - set(raw))
        extra = sorted(set(raw) - expected_root)
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if extra:
            details.append("unexpected " + ", ".join(extra))
        raise ValueError("Invalid Airtable cycle policy: " + "; ".join(details))
    if raw["policy_version"] != 1:
        raise ValueError("Airtable cycle policy version must be 1")
    if raw["default_cycle"] != "6th":
        raise ValueError("Airtable cycle policy default_cycle must be 6th")
    expected_count = raw["expected_jurisdiction_count"]
    if (
        isinstance(expected_count, bool)
        or not isinstance(expected_count, int)
        or expected_count < 1
    ):
        raise ValueError("expected_jurisdiction_count must be a positive integer")
    if len(sixth_cycle_periods) != expected_count:
        raise ValueError(
            "Sixth-cycle jurisdiction count does not match the Airtable cycle policy: "
            f"{len(sixth_cycle_periods)} != {expected_count}"
        )
    source = raw["source"]
    if not isinstance(source, str) or not source.startswith("https://www.hcd.ca.gov/"):
        raise ValueError("Airtable cycle policy source must be an HCD HTTPS URL")

    expected_cycle_counts = raw["expected_cycle_counts"]
    if not isinstance(expected_cycle_counts, dict) or set(expected_cycle_counts) != {
        "6th",
        "7th",
    }:
        raise ValueError("expected_cycle_counts must contain only 6th and 7th")
    for cycle, count in expected_cycle_counts.items():
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError(f"expected_cycle_counts.{cycle} must be nonnegative")
    if sum(expected_cycle_counts.values()) != expected_count:
        raise ValueError("Airtable cycle policy counts do not reconcile")

    overrides = raw["overrides"]
    if not isinstance(overrides, list):
        raise TypeError("Airtable cycle policy overrides must be an array")
    periods = dict(sixth_cycle_periods)
    cycles = {key: "6th" for key in periods}
    seen: set[str] = set()
    override_fields = {"jurisdiction_key", "cycle", "start", "end"}
    for position, item in enumerate(overrides):
        if not isinstance(item, dict) or set(item) != override_fields:
            raise ValueError(f"Invalid Airtable cycle override at index {position}")
        key = item["jurisdiction_key"]
        if not isinstance(key, str) or normalized_key(key) != key:
            raise ValueError(f"Override {position} has a noncanonical jurisdiction key")
        if key not in periods:
            raise ValueError(
                f"Override {position} names an unknown jurisdiction: {key}"
            )
        if key in seen:
            raise ValueError(f"Duplicate Airtable cycle override: {key}")
        seen.add(key)
        if item["cycle"] != "7th":
            raise ValueError(f"Override {position} cycle must be 7th")
        try:
            start = dt.date.fromisoformat(str(item["start"]))
            end = dt.date.fromisoformat(str(item["end"]))
        except ValueError as error:
            raise ValueError(f"Override {position} has an invalid date") from error
        if start.isoformat() != item["start"] or end.isoformat() != item["end"]:
            raise ValueError(f"Override {position} dates must be canonical ISO dates")
        if start > end:
            raise ValueError(f"Override {position} starts after it ends")
        previous = periods[key]
        periods[key] = PlanningPeriod(
            key=key,
            display=previous.display,
            start=start,
            end=end,
            cycle_started=True,
        )
        cycles[key] = "7th"

    actual_counts = Counter(cycles.values())
    if dict(sorted(actual_counts.items())) != dict(
        sorted(expected_cycle_counts.items())
    ):
        raise ValueError(
            "Airtable cycle policy overrides do not match expected_cycle_counts"
        )
    policy_sha256 = hashlib.sha256(
        json.dumps(
            raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    ).hexdigest()
    return periods, cycles, policy_sha256


def build_airtable_artifacts(
    *,
    table_a2_path: Path,
    rhna_path: Path,
    table_contract_path: Path,
    rhna_contract_path: Path,
    output_schema_path: Path,
    cycle_policy_path: Path,
    source_manifest: Mapping[str, object],
    cutoff_year: int,
    output_dir: Path,
    audit_dir: Path,
) -> AirtableBuildArtifacts:
    """Build cycle-aware, reviewed permit and entitlement totals for Airtable.

    Permits and entitlements are selected in one pass over Table A2 and are
    deduplicated independently. The entitlement fields are merged into the
    permit payload only after the permit projection is rendered, and the build
    fails unless removing them reproduces that projection byte for byte.
    """

    table_contract = load_contract(table_contract_path)
    rhna_contract = load_contract(rhna_contract_path)
    sixth_periods = load_planning_periods(rhna_path, rhna_contract)
    periods, cycles, policy_sha256 = _load_airtable_cycle_policy(
        cycle_policy_path, sixth_periods
    )
    (
        (selected, selection_stats),
        (entitlement_selected, entitlement_selection_stats),
    ) = select_activity_records(
        table_a2_path,
        table_contract,
        periods,
        cutoff_year,
        (PERMIT_ACTIVITY, ENTITLEMENT_ACTIVITY),
    )
    result = deduplicate_activity(PERMIT_ACTIVITY, selected)
    if len(selected) != len(result.retained) + len(result.removed):
        raise AssertionError("Airtable row conservation failed after deduplication")
    entitlement_result = deduplicate_activity(
        ENTITLEMENT_ACTIVITY, entitlement_selected
    )
    if len(entitlement_selected) != len(entitlement_result.retained) + len(
        entitlement_result.removed
    ):
        raise AssertionError(
            "Airtable entitlement row conservation failed after deduplication"
        )

    retained_by_jurisdiction: dict[str, list[PermitRecord]] = defaultdict(list)
    for record in result.retained:
        retained_by_jurisdiction[record.jurisdiction].append(record)
    last_updated = _source_last_updated(source_manifest)
    complete_after = dt.date(cutoff_year + 1, 6, 30)
    provisional = dt.date.fromisoformat(last_updated) <= complete_after
    manifest_sha = _manifest_digest(source_manifest)

    jurisdiction_rows: list[dict[str, object]] = []
    for key, period in sorted(
        periods.items(), key=lambda item: item[1].display.casefold()
    ):
        records = retained_by_jurisdiction.get(key, [])
        categories = _vector_sum(records)
        jurisdiction_rows.append(
            {
                "jurisdiction": period.display,
                "jurisdiction_key": key,
                "cycle": cycles[key],
                "period_start": period.start.isoformat(),
                "period_end": period.end.isoformat(),
                **dict(zip(CATEGORY_NAMES, categories)),
                "total": sum(categories),
                "undated_permits": sum(
                    record.units for record in records if record.undated
                ),
                "last_updated": last_updated,
                "data_status": "reported" if records else "no_selected_rows",
            }
        )

    selected_categories = _vector_sum(selected)
    retained_categories = _vector_sum(result.retained)
    removed_categories = _vector_sum(result.removed)
    if (
        tuple(
            retained + removed
            for retained, removed in zip(retained_categories, removed_categories)
        )
        != selected_categories
    ):
        raise AssertionError("Airtable category totals do not reconcile")

    cycle_counts = dict(sorted(Counter(cycles.values()).items()))
    metadata = {
        "cutoff_year": cutoff_year,
        "last_updated": last_updated,
        "provisional": provisional,
        "complete_after": complete_after.isoformat(),
        "source_manifest_sha256": manifest_sha,
        "dedupe_profile": DEDUPE_PROFILE,
        "cycle_policy_sha256": policy_sha256,
        "cycle_counts": cycle_counts,
        "jurisdiction_count": len(jurisdiction_rows),
        "selected_row_count": len(selected),
        "selected_units": sum(selected_categories),
        "retained_row_count": len(result.retained),
        "removed_row_count": len(result.removed),
        "removed_units": sum(removed_categories),
        "undated_permits": sum(
            record.units for record in result.retained if record.undated
        ),
    }
    jurisdiction_payload = {
        "metadata": metadata,
        "jurisdictions": jurisdiction_rows,
    }
    audit_payload = {
        "metadata": metadata,
        "selection": selection_stats,
        "selected_categories": dict(zip(CATEGORY_NAMES, selected_categories)),
        "retained_categories": dict(zip(CATEGORY_NAMES, retained_categories)),
        "removed_categories": dict(zip(CATEGORY_NAMES, removed_categories)),
        "removed_by_rule": _removal_rule_stats(result),
        "invariants": {
            "row_conservation": True,
            "category_conservation": True,
            "jurisdiction_aggregation_reconciles": True,
            "cycle_counts_reconcile": sum(cycle_counts.values())
            == len(jurisdiction_rows),
        },
    }
    permit_text = _json_text(jurisdiction_payload)

    entitlements_by_jurisdiction: dict[str, list[PermitRecord]] = defaultdict(list)
    for record in entitlement_result.retained:
        entitlements_by_jurisdiction[record.jurisdiction].append(record)
    entitlement_fields: dict[str, dict[str, object]] = {}
    for key in periods:
        records = entitlements_by_jurisdiction.get(key, [])
        categories = _vector_sum(records)
        entitlement_fields[key] = {
            **{
                f"entitlement_{name}": value
                for name, value in zip(CATEGORY_NAMES, categories)
            },
            "entitlement_total": sum(categories),
            "undated_entitlements": sum(
                record.units for record in records if record.undated
            ),
            "entitlement_data_status": "reported" if records else "no_selected_rows",
        }

    entitlement_selected_categories = _vector_sum(entitlement_selected)
    entitlement_retained_categories = _vector_sum(entitlement_result.retained)
    entitlement_removed_categories = _vector_sum(entitlement_result.removed)
    if (
        tuple(
            retained + removed
            for retained, removed in zip(
                entitlement_retained_categories, entitlement_removed_categories
            )
        )
        != entitlement_selected_categories
    ):
        raise AssertionError("Airtable entitlement category totals do not reconcile")
    if (
        tuple(
            sum(fields[f"entitlement_{name}"] for fields in entitlement_fields.values())
            for name in CATEGORY_NAMES
        )
        != entitlement_retained_categories
    ):
        raise AssertionError("Airtable entitlement aggregation does not reconcile")

    entitlement_metadata = {
        "entitlement_selected_row_count": len(entitlement_selected),
        "entitlement_selected_units": sum(entitlement_selected_categories),
        "entitlement_retained_row_count": len(entitlement_result.retained),
        "entitlement_removed_row_count": len(entitlement_result.removed),
        "entitlement_removed_units": sum(entitlement_removed_categories),
        "undated_entitlements": sum(
            record.units for record in entitlement_result.retained if record.undated
        ),
    }
    totals_payload = {
        "metadata": {**metadata, **entitlement_metadata},
        "jurisdictions": [
            {**row, **entitlement_fields[str(row["jurisdiction_key"])]}
            for row in jurisdiction_rows
        ],
    }
    permit_projection = {
        "metadata": {
            key: value
            for key, value in totals_payload["metadata"].items()
            if key not in ENTITLEMENT_METADATA_FIELDS
        },
        "jurisdictions": [
            {
                key: value
                for key, value in row.items()
                if key not in ENTITLEMENT_ROW_FIELDS
            }
            for row in totals_payload["jurisdictions"]
        ],
    }
    if _json_text(permit_projection) != permit_text:
        raise AssertionError(
            "Entitlement fields changed the Airtable permit projection"
        )

    entitlement_audit_payload = {
        "metadata": totals_payload["metadata"],
        "source_columns": {
            "date": ENTITLEMENT_ACTIVITY.date_field,
            "total": ENTITLEMENT_ACTIVITY.total_field,
            "categories": {
                name: list(group)
                for name, group in zip(
                    CATEGORY_NAMES, ENTITLEMENT_ACTIVITY.category_fields
                )
            },
        },
        "selection": entitlement_selection_stats,
        "selected_categories": dict(
            zip(CATEGORY_NAMES, entitlement_selected_categories)
        ),
        "retained_categories": dict(
            zip(CATEGORY_NAMES, entitlement_retained_categories)
        ),
        "removed_categories": dict(zip(CATEGORY_NAMES, entitlement_removed_categories)),
        "removed_by_rule": _removal_rule_stats(entitlement_result),
        "invariants": {
            "row_conservation": True,
            "category_conservation": True,
            "jurisdiction_aggregation_reconciles": True,
            "permit_projection_unchanged": True,
            "cycle_counts_reconcile": sum(cycle_counts.values())
            == len(jurisdiction_rows),
        },
    }
    validate_json_document(totals_payload, output_schema_path)

    jurisdiction_json = output_dir / "airtable_totals.json"
    audit_summary = output_dir / "airtable_audit_summary.json"
    decision_ledger = audit_dir / "airtable_dedupe_decisions.jsonl"
    review_ledger = audit_dir / "airtable_dedupe_review.jsonl"
    entitlement_audit_summary = output_dir / "airtable_entitlement_audit_summary.json"
    entitlement_decision_ledger = audit_dir / "airtable_entitlement_decisions.jsonl"
    entitlement_review_ledger = audit_dir / "airtable_entitlement_review.jsonl"
    _atomic_write(jurisdiction_json, _json_text(totals_payload))
    _atomic_write(audit_summary, _json_text(audit_payload))
    _atomic_write(entitlement_audit_summary, _json_text(entitlement_audit_payload))
    _write_decision_ledgers(
        selected=selected,
        result=result,
        decision_ledger=decision_ledger,
        review_ledger=review_ledger,
        cycles=cycles,
    )
    _write_decision_ledgers(
        selected=entitlement_selected,
        result=entitlement_result,
        decision_ledger=entitlement_decision_ledger,
        review_ledger=entitlement_review_ledger,
        cycles=cycles,
        date_key="entitlement_date",
    )

    return AirtableBuildArtifacts(
        jurisdiction_json=jurisdiction_json,
        audit_summary=audit_summary,
        decision_ledger=decision_ledger,
        review_ledger=review_ledger,
        selected_rows=len(selected),
        retained_rows=len(result.retained),
        removed_rows=len(result.removed),
        total_units=sum(retained_categories),
        entitlement_audit_summary=entitlement_audit_summary,
        entitlement_decision_ledger=entitlement_decision_ledger,
        entitlement_review_ledger=entitlement_review_ledger,
        entitlement_selected_rows=len(entitlement_selected),
        entitlement_retained_rows=len(entitlement_result.retained),
        entitlement_removed_rows=len(entitlement_result.removed),
        entitlement_total_units=sum(entitlement_retained_categories),
    )


def build_artifacts(
    *,
    table_a2_path: Path,
    rhna_path: Path,
    table_contract_path: Path,
    rhna_contract_path: Path,
    output_schema_path: Path,
    source_manifest: Mapping[str, object],
    cutoff_year: int,
    output_dir: Path,
    audit_dir: Path,
) -> BuildArtifacts:
    table_contract = load_contract(table_contract_path)
    rhna_contract = load_contract(rhna_contract_path)
    sources = source_manifest.get("sources")
    if not isinstance(sources, Mapping):
        raise TypeError("Source manifest has no sources mapping")
    table_source = sources.get("table_a2")
    rhna_source = sources.get("rhna_progress_6")
    if not isinstance(table_source, Mapping) or not isinstance(rhna_source, Mapping):
        raise TypeError("Source manifest is missing required source entries")
    _verify_manifest_source(
        label="Table A2", path=table_a2_path, source_entry=table_source
    )
    _verify_manifest_source(label="RHNA", path=rhna_path, source_entry=rhna_source)
    periods = load_planning_periods(rhna_path, rhna_contract)
    selected, selection_stats = select_permit_records(
        table_a2_path,
        table_contract,
        periods,
        cutoff_year,
    )
    table_total = table_source.get("datastore_total")
    if table_total is not None and table_total != selection_stats["raw_row_count"]:
        raise ValueError(
            "Table A2 raw record count does not match the DataStore total: "
            f"{selection_stats['raw_row_count']} != {table_total}"
        )
    rhna_total = rhna_source.get("datastore_total")
    if rhna_total is not None and rhna_total != len(periods):
        raise ValueError(
            "RHNA raw record count does not match the DataStore total: "
            f"{len(periods)} != {rhna_total}"
        )
    for label, source_entry, contract in (
        ("Table A2", table_source, table_contract),
        ("RHNA", rhna_source, rhna_contract),
    ):
        datastore_fields = source_entry.get("datastore_fields")
        if (
            datastore_fields is not None
            and contract.ordered_columns is not None
            and tuple(datastore_fields) != contract.ordered_columns
        ):
            raise ValueError(
                f"{label} DataStore fields do not match the pinned CSV header"
            )
    result = deduplicate_activity(PERMIT_ACTIVITY, selected)
    if len(selected) != len(result.retained) + len(result.removed):
        raise AssertionError("Selected rows do not reconcile with deduplication result")

    retained_by_jurisdiction: dict[str, list[PermitRecord]] = defaultdict(list)
    for record in result.retained:
        retained_by_jurisdiction[record.jurisdiction].append(record)
    last_updated = _source_last_updated(source_manifest)
    complete_after = dt.date(cutoff_year + 1, 6, 30)
    provisional = dt.date.fromisoformat(last_updated) <= complete_after
    manifest_sha = _manifest_digest(source_manifest)
    jurisdiction_rows: list[dict[str, object]] = []
    for key, period in sorted(
        periods.items(), key=lambda item: item[1].display.casefold()
    ):
        records = retained_by_jurisdiction.get(key, [])
        categories = _vector_sum(records)
        row: dict[str, object] = {
            "jurisdiction": period.display,
            "jurisdiction_key": key,
            **dict(zip(CATEGORY_NAMES, categories)),
            "total": sum(categories),
            "undated_permits": sum(
                record.units for record in records if record.undated
            ),
            "last_updated": last_updated,
            "data_status": "reported" if records else "no_selected_rows",
        }
        jurisdiction_rows.append(row)

    retained_categories = _vector_sum(result.retained)
    removed_categories = _vector_sum(result.removed)
    selected_categories = _vector_sum(selected)
    if (
        tuple(
            kept + removed
            for kept, removed in zip(retained_categories, removed_categories)
        )
        != selected_categories
    ):
        raise AssertionError("Category totals do not reconcile after deduplication")
    if (
        _vector_sum(
            tuple(
                record for rows in retained_by_jurisdiction.values() for record in rows
            )
        )
        != retained_categories
    ):
        raise AssertionError("Jurisdiction aggregation does not reconcile")

    rule_stats = _removal_rule_stats(result)

    metadata = {
        "cutoff_year": cutoff_year,
        "last_updated": last_updated,
        "provisional": provisional,
        "complete_after": complete_after.isoformat(),
        "source_manifest_sha256": manifest_sha,
        "dedupe_profile": DEDUPE_PROFILE,
        "jurisdiction_count": len(jurisdiction_rows),
        "selected_row_count": len(selected),
        "selected_units": sum(selected_categories),
        "retained_row_count": len(result.retained),
        "removed_row_count": len(result.removed),
        "removed_units": sum(removed_categories),
        "undated_permits": sum(
            record.units for record in result.retained if record.undated
        ),
    }
    jurisdiction_payload = {
        "metadata": metadata,
        "jurisdictions": jurisdiction_rows,
    }
    audit_payload = {
        "metadata": metadata,
        "selection": selection_stats,
        "selected_categories": dict(zip(CATEGORY_NAMES, selected_categories)),
        "retained_categories": dict(zip(CATEGORY_NAMES, retained_categories)),
        "removed_categories": dict(zip(CATEGORY_NAMES, removed_categories)),
        "removed_by_rule": rule_stats,
        "invariants": {
            "row_conservation": True,
            "category_conservation": True,
            "jurisdiction_aggregation_reconciles": True,
        },
    }
    validate_json_document(jurisdiction_payload, output_schema_path)

    output_dir.mkdir(parents=True, exist_ok=True)
    audit_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "source_manifest.json"
    jurisdiction_json = output_dir / "jurisdiction_totals.json"
    jurisdiction_csv = output_dir / "jurisdiction_totals.csv"
    audit_summary = output_dir / "audit_summary.json"
    decision_ledger = audit_dir / "dedupe_decisions.jsonl"
    review_ledger = audit_dir / "dedupe_review.jsonl"
    _atomic_write(manifest_path, _json_text(source_manifest))
    _atomic_write(jurisdiction_json, _json_text(jurisdiction_payload))
    _atomic_write(audit_summary, _json_text(audit_payload))

    csv_buffer = io.StringIO(newline="")
    writer = csv.DictWriter(csv_buffer, fieldnames=CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for row in jurisdiction_rows:
        writer.writerow(
            {
                "Jurisdiction": row["jurisdiction"],
                "Acutely Low": row["acutely_low"],
                "Extremely Low": row["extremely_low"],
                "Very Low": row["very_low"],
                "Low": row["low"],
                "Moderate": row["moderate"],
                "Above Moderate": row["above_moderate"],
                "Total": row["total"],
                "Undated Permits": row["undated_permits"],
                "Last Updated": row["last_updated"],
            }
        )
    _atomic_write(jurisdiction_csv, csv_buffer.getvalue())

    _write_decision_ledgers(
        selected=selected,
        result=result,
        decision_ledger=decision_ledger,
        review_ledger=review_ledger,
    )

    return BuildArtifacts(
        jurisdiction_json=jurisdiction_json,
        jurisdiction_csv=jurisdiction_csv,
        audit_summary=audit_summary,
        decision_ledger=decision_ledger,
        review_ledger=review_ledger,
        selected_rows=len(selected),
        retained_rows=len(result.retained),
        removed_rows=len(result.removed),
        total_units=sum(retained_categories),
    )
