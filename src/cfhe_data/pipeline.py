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

from .dedupe import deduplicate
from .models import (
    DEFAULT_CATEGORY_FIELDS,
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

DEDUPE_PROFILE = "audited-apr-snapshots-v3"
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


def _strict_categories(row: Mapping[str, str], record_index: int) -> tuple[int, ...]:
    return tuple(
        sum(
            parse_nonnegative_integer(row[field], field=field, record=record_index)
            for field in group
        )
        for group in DEFAULT_CATEGORY_FIELDS
    )


def select_permit_records(
    path: Path,
    contract: CsvContract,
    periods: Mapping[str, PlanningPeriod],
    cutoff_year: int,
) -> tuple[tuple[PermitRecord, ...], dict[str, object]]:
    if cutoff_year < 2018:
        raise ValueError("cutoff_year must be 2018 or later")
    cutoff_date = dt.date(cutoff_year, 12, 31)
    records: list[PermitRecord] = []
    raw_jurisdictions: set[str] = set()
    selected_by_jurisdiction: Counter[str] = Counter()
    mismatch_rows = 0
    mismatch_units = 0
    raw_row_count = 0
    max_report_year = 0

    with open_validated_csv(path, contract) as opened:
        fingerprint_fields = opened.fieldnames
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
            period = periods.get(jurisdiction)
            if period is None:
                continue
            permit_date = parse_date(
                row["BP_ISSUE_DT1"],
                field="BP_ISSUE_DT1",
                record=record_index,
            )
            effective_end = min(period.end, cutoff_date)
            if permit_date is None:
                included = period.start.year <= report_year <= effective_end.year
            else:
                included = period.start <= permit_date <= effective_end
            if not included:
                continue
            categories = _strict_categories(row, record_index)
            reported_total = parse_nonnegative_integer(
                row["NO_BUILDING_PERMITS"],
                field="NO_BUILDING_PERMITS",
                record=record_index,
            )
            category_total = sum(categories)
            if not (category_total or reported_total):
                continue
            if category_total != reported_total:
                mismatch_rows += 1
                mismatch_units += category_total - reported_total
            records.append(
                PermitRecord.from_values(
                    source_record_index=record_index,
                    csv_physical_line=physical_line,
                    report_year=report_year,
                    jurisdiction=jurisdiction,
                    jurisdiction_display=period.display,
                    permit_date=permit_date,
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
            )
            selected_by_jurisdiction[jurisdiction] += 1

    stats = {
        "raw_row_count": raw_row_count,
        "raw_jurisdiction_count": len(raw_jurisdictions),
        "raw_jurisdictions_without_planning_period": sorted(
            raw_jurisdictions - set(periods)
        ),
        "planning_periods_without_selected_rows": sorted(
            key for key in periods if selected_by_jurisdiction[key] == 0
        ),
        "max_report_year": max_report_year,
        "selected_row_count": len(records),
        "reported_total_mismatch_rows": mismatch_rows,
        "reported_total_net_difference": mismatch_units,
    }
    if mismatch_rows:
        raise ValueError(
            f"{mismatch_rows} selected Table A2 rows do not reconcile with "
            f"NO_BUILDING_PERMITS; net category difference {mismatch_units}"
        )
    return tuple(records), stats


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
        "permit_date": record.permit_date.isoformat() if record.permit_date else None,
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
        ledger_row = _decision_ledger_row(record, audit, cycle=cycle)
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
    """Build a cycle-aware, reviewed permit artifact for Airtable publication."""

    table_contract = load_contract(table_contract_path)
    rhna_contract = load_contract(rhna_contract_path)
    sixth_periods = load_planning_periods(rhna_path, rhna_contract)
    periods, cycles, policy_sha256 = _load_airtable_cycle_policy(
        cycle_policy_path, sixth_periods
    )
    selected, selection_stats = select_permit_records(
        table_a2_path, table_contract, periods, cutoff_year
    )
    result = deduplicate(selected)
    if len(selected) != len(result.retained) + len(result.removed):
        raise AssertionError("Airtable row conservation failed after deduplication")

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
    validate_json_document(jurisdiction_payload, output_schema_path)

    jurisdiction_json = output_dir / "airtable_totals.json"
    audit_summary = output_dir / "airtable_audit_summary.json"
    decision_ledger = audit_dir / "airtable_dedupe_decisions.jsonl"
    review_ledger = audit_dir / "airtable_dedupe_review.jsonl"
    _atomic_write(jurisdiction_json, _json_text(jurisdiction_payload))
    _atomic_write(audit_summary, _json_text(audit_payload))
    _write_decision_ledgers(
        selected=selected,
        result=result,
        decision_ledger=decision_ledger,
        review_ledger=review_ledger,
        cycles=cycles,
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
    result = deduplicate(selected)
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
