# visibility: public
"""Quarterly Table A2 series for the Airtable "APR Quarterly" table.

Every jurisdiction in the cycle policy gets one row per calendar quarter from
2018 through the cutoff year. Entitlements, building permits, and completions
are selected over that one window in a single pass, deduplicated under the
same profiles the cycle totals use, and bucketed by each stage's own activity
date. Quarters with no activity keep explicit zeros, so a missing quarter
never reads as a missing report.

The series reconciles to ``airtable_totals.json``: summing a jurisdiction's
dated quarters inside its cycle window must reproduce the published dated
totals. A mismatch never fails the build; it is recorded in
``metadata.sync_blockers``, and the series sync refuses to write while any
blocker is present.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .models import PermitRecord
from .normalize import normalized_key, parse_number
from .pipeline import (
    COMPLETION_ACTIVITY,
    DEDUPE_PROFILE,
    ENTITLEMENT_ACTIVITY,
    PERMIT_ACTIVITY,
    ActivitySpec,
    PlanningPeriod,
    _atomic_write,
    _load_airtable_cycle_policy,
    _manifest_digest,
    _removal_rule_stats,
    _source_last_updated,
    deduplicate_activity,
    load_planning_periods,
    select_activity_records,
)
from .schema import load_contract, validate_json_document

SERIES_START = dt.date(2018, 1, 1)
SERIES_STAGES: tuple[tuple[str, str, ActivitySpec], ...] = (
    ("entitlements", "entitlement", ENTITLEMENT_ACTIVITY),
    ("permits", "permit", PERMIT_ACTIVITY),
    ("completions", "completion", COMPLETION_ACTIVITY),
)
STAGE_MEASURES = (
    "vli",
    "li",
    "mi",
    "ami",
    "total",
    "deed_restricted",
    "sfd",
    "sfa",
    "2_to_4",
    "5_plus",
    "adu",
    "mh",
    "sb35",
    "sb9",
    "ab2011",
    "density_bonus",
)
SERIES_COLUMNS = (
    "key",
    "jurisdiction",
    "jurisdiction_key",
    "year",
    "quarter",
    "quarter_start",
    "table_a2_rows",
    "data_flags",
    *(
        f"{stage}_{measure}"
        for stage, _singular, _activity in SERIES_STAGES
        for measure in STAGE_MEASURES
    ),
)
UNIT_CATEGORY_MEASURES = {
    "SFD": "sfd",
    "SFA": "sfa",
    "2 TO 4": "2_to_4",
    "5+": "5_plus",
    "ADU": "adu",
    "MH": "mh",
}
# APPROVE_SB35 labels, keyed by normalized_key. SB 423 extended SB 35's
# streamlined approval, so both count as SB 35, as does the older Y flag.
PROGRAM_LABEL_MEASURES = {
    "Y": "sb35",
    "SB 35 (2017)": "sb35",
    "SB 423 (2023)": "sb35",
    "SMAP": "sb35",
    "SB 9 (2021) - DUPLEX IN SF ZONE": "sb9",
    "SB 9 (2021) - RESIDENTIAL LOT SPLIT": "sb9",
    "AB 2011 (2022)": "ab2011",
}
NO_PROGRAM_LABELS = frozenset({"", "N", "NONE", "0"})
# Labels the series reports but does not give a column.
SKIPPED_PROGRAM_LABELS = frozenset({"SB 4 (2023)", "SB 6 (2022)"})
PROGRAM_FIELD = "APPROVE_SB35"
DENSITY_BONUS_FIELD = "DENSITY_BONUS_TOTAL"
DATA_FLAG_ORDER = (
    "no_apr_rows",
    "permit_dates_heaped",
    "entitlement_dates_heaped",
    "completion_dates_heaped",
    "provisional_year",
)
HEAPING_MIN_ROWS = 10
HEAPING_MIN_SHARE = 0.5
RECONCILED_TOTALS = {
    "entitlements": ("entitlement_total", "undated_entitlements"),
    "permits": ("total", "undated_permits"),
}
TOTALS_IDENTITY_FIELDS = (
    "cutoff_year",
    "source_manifest_sha256",
    "dedupe_profile",
    "cycle_policy_sha256",
)


@dataclass(frozen=True, slots=True)
class SeriesBuildArtifacts:
    """The quarterly series file and its headline counts."""

    series_json: Path
    jurisdiction_count: int
    row_count: int
    sync_blocker_count: int
    entitlement_units: int
    permit_units: int
    completion_units: int


def _quarter(date: dt.date) -> int:
    return (date.month - 1) // 3 + 1


def _program_labels(value: object) -> tuple[str, ...]:
    labels = {normalized_key(part) for part in re.split(r"[;,]", str(value or ""))}
    return tuple(sorted(labels - NO_PROGRAM_LABELS))


def _deed_restricted(record: PermitRecord, activity: ActivitySpec) -> int:
    raw = record.raw_row
    return sum(
        parse_number(raw[field])
        for group in activity.category_fields
        for field in group
        if field.endswith("_DR")
    )


@dataclass(slots=True)
class _StageTally:
    """Per-stage accumulators gathered while bucketing retained records."""

    cells: dict[tuple[str, int, int], list[int]]
    undated_by_jurisdiction: Counter[str]
    dates_by_year: dict[tuple[str, int], Counter[dt.date]]
    in_cycle_by_jurisdiction: Counter[str]
    program_label_rows: Counter[str]
    unrecognized_unit_categories: Counter[str]
    density_bonus_unparsed_rows: int = 0


def _tally_stage(
    activity: ActivitySpec,
    retained: Sequence[PermitRecord],
    cycle_periods: Mapping[str, PlanningPeriod],
    cutoff_date: dt.date,
) -> _StageTally:
    measure_index = {measure: index for index, measure in enumerate(STAGE_MEASURES)}
    tally = _StageTally(
        cells=defaultdict(lambda: [0] * len(STAGE_MEASURES)),
        undated_by_jurisdiction=Counter(),
        dates_by_year=defaultdict(Counter),
        in_cycle_by_jurisdiction=Counter(),
        program_label_rows=Counter(),
        unrecognized_unit_categories=Counter(),
    )
    for record in retained:
        if record.undated:
            tally.undated_by_jurisdiction[record.jurisdiction] += record.units
            continue
        date = record.permit_date
        assert date is not None
        cell = tally.cells[(record.jurisdiction, date.year, _quarter(date))]
        categories = record.categories
        units = record.units
        cell[measure_index["vli"]] += categories[0] + categories[1] + categories[2]
        cell[measure_index["li"]] += categories[3]
        cell[measure_index["mi"]] += categories[4]
        cell[measure_index["ami"]] += categories[5]
        cell[measure_index["total"]] += units
        cell[measure_index["deed_restricted"]] += _deed_restricted(record, activity)
        unit_measure = UNIT_CATEGORY_MEASURES.get(record.unit_category)
        if unit_measure is None:
            tally.unrecognized_unit_categories[record.unit_category] += 1
        else:
            cell[measure_index[unit_measure]] += units
        raw = record.raw_row
        programs: set[str] = set()
        for label in _program_labels(raw.get(PROGRAM_FIELD)):
            tally.program_label_rows[label] += 1
            measure = PROGRAM_LABEL_MEASURES.get(label)
            if measure is not None:
                programs.add(measure)
        for measure in programs:
            cell[measure_index[measure]] += units
        try:
            density_bonus = parse_number(raw.get(DENSITY_BONUS_FIELD))
        except ValueError:
            tally.density_bonus_unparsed_rows += 1
            density_bonus = 0
        if density_bonus > 0:
            cell[measure_index["density_bonus"]] += units
        tally.dates_by_year[(record.jurisdiction, date.year)][date] += 1
        period = cycle_periods[record.jurisdiction]
        if period.start <= date <= min(period.end, cutoff_date):
            tally.in_cycle_by_jurisdiction[record.jurisdiction] += units
    return tally


def _heaping(
    singular: str, tally: _StageTally
) -> tuple[list[dict[str, object]], set[tuple[str, int]]]:
    entries: list[dict[str, object]] = []
    heaped: set[tuple[str, int]] = set()
    for (key, year), dates in sorted(tally.dates_by_year.items()):
        dated_rows = sum(dates.values())
        if dated_rows < HEAPING_MIN_ROWS:
            continue
        date, rows = min(dates.items(), key=lambda item: (-item[1], item[0]))
        share = rows / dated_rows
        if share < HEAPING_MIN_SHARE:
            continue
        heaped.add((key, year))
        entries.append(
            {
                "stage": singular,
                "jurisdiction_key": key,
                "year": year,
                "date": date.isoformat(),
                "rows": rows,
                "dated_rows": dated_rows,
                "share": round(share, 4),
            }
        )
    return entries, heaped


def _reconcile(
    totals: Mapping[str, object],
    metadata: Mapping[str, object],
    periods: Mapping[str, PlanningPeriod],
    tallies: Mapping[str, _StageTally],
) -> tuple[list[str], dict[str, dict[str, int]]]:
    """Compare in-cycle dated quarters with the published cycle totals."""

    blockers: list[str] = []
    totals_metadata = totals.get("metadata")
    totals_rows = totals.get("jurisdictions")
    if not isinstance(totals_metadata, Mapping) or not isinstance(totals_rows, list):
        return ["airtable_totals.json has no metadata or jurisdictions"], {}
    for field in TOTALS_IDENTITY_FIELDS:
        if totals_metadata.get(field) != metadata[field]:
            blockers.append(
                f"airtable_totals.json {field} differs from the series inputs"
            )
    by_key = {
        str(row.get("jurisdiction_key")): row
        for row in totals_rows
        if isinstance(row, Mapping)
    }
    if set(by_key) != set(periods):
        blockers.append("airtable_totals.json jurisdictions differ from the series")
    summary: dict[str, dict[str, int]] = {}
    for stage, (total_field, undated_field) in RECONCILED_TOTALS.items():
        tally = tallies[stage]
        mismatched = 0
        net = 0
        for key in sorted(periods):
            row = by_key.get(key)
            if row is None:
                continue
            expected = int(row[total_field]) - int(row[undated_field])
            actual = tally.in_cycle_by_jurisdiction.get(key, 0)
            if actual != expected:
                mismatched += 1
                net += actual - expected
                blockers.append(
                    f"{stage} for {key}: in-cycle quarters sum to {actual}, "
                    f"published dated total is {expected}"
                )
        summary[stage] = {
            "jurisdictions_checked": sum(1 for key in periods if key in by_key),
            "mismatched_jurisdictions": mismatched,
            "net_difference": net,
        }
    return blockers, summary


def _series_text(payload: Mapping[str, object]) -> str:
    """Render the series with each row on one line, sorted keys elsewhere."""

    head = json.dumps(
        {key: value for key, value in payload.items() if key != "rows"},
        indent=2,
        sort_keys=True,
        ensure_ascii=False,
    )
    if not head.endswith("\n}") or any(key > "rows" for key in payload):
        raise AssertionError("Series payload keys do not render in sorted order")
    rows = payload["rows"]
    assert isinstance(rows, list)
    body = ",\n".join(
        "    " + json.dumps(row, ensure_ascii=False, separators=(", ", ": "))
        for row in rows
    )
    text = head[:-2] + ',\n  "rows": [\n' + body + ("\n" if body else "") + "  ]\n}\n"
    if json.loads(text) != payload:
        raise AssertionError("Series rendering does not round trip")
    return text


def build_series_artifacts(
    *,
    table_a2_path: Path,
    rhna_path: Path,
    table_contract_path: Path,
    rhna_contract_path: Path,
    cycle_policy_path: Path,
    series_schema_path: Path,
    totals_path: Path,
    source_manifest: Mapping[str, object],
    cutoff_year: int,
    output_dir: Path,
) -> SeriesBuildArtifacts:
    """Build the quarterly series and reconcile it to the cycle totals."""

    table_contract = load_contract(table_contract_path)
    rhna_contract = load_contract(rhna_contract_path)
    sixth_periods = load_planning_periods(rhna_path, rhna_contract)
    cycle_periods, cycles, policy_sha256 = _load_airtable_cycle_policy(
        cycle_policy_path, sixth_periods
    )
    cutoff_date = dt.date(cutoff_year, 12, 31)
    window = {
        key: PlanningPeriod(
            key=key,
            display=period.display,
            start=SERIES_START,
            end=cutoff_date,
            cycle_started=True,
        )
        for key, period in cycle_periods.items()
    }
    displays = [period.display for period in window.values()]
    if len(set(displays)) != len(displays):
        raise ValueError("Series jurisdiction display names are not unique")

    report_year_counts: Counter[tuple[str, int]] = Counter()
    selections = select_activity_records(
        table_a2_path,
        table_contract,
        window,
        cutoff_year,
        tuple(activity for _stage, _singular, activity in SERIES_STAGES),
        report_year_counts=report_year_counts,
    )

    last_updated = _source_last_updated(source_manifest)
    metadata: dict[str, object] = {
        "cutoff_year": cutoff_year,
        "last_updated": last_updated,
        "source_manifest_sha256": _manifest_digest(source_manifest),
        "dedupe_profile": DEDUPE_PROFILE,
        "cycle_policy_sha256": policy_sha256,
        "cycle_counts": dict(sorted(Counter(cycles.values()).items())),
        "window_start": SERIES_START.isoformat(),
        "window_end": cutoff_date.isoformat(),
    }

    tallies: dict[str, _StageTally] = {}
    stage_stats: dict[str, dict[str, object]] = {}
    heaping_entries: list[dict[str, object]] = []
    heaped: dict[str, set[tuple[str, int]]] = {}
    for (stage, singular, activity), (selected, selection_stats) in zip(
        SERIES_STAGES, selections
    ):
        result = deduplicate_activity(activity, selected)
        if len(selected) != len(result.retained) + len(result.removed):
            raise AssertionError(f"Series {stage} row conservation failed")
        tally = _tally_stage(activity, result.retained, cycle_periods, cutoff_date)
        tallies[stage] = tally
        entries, heaped[singular] = _heaping(singular, tally)
        heaping_entries.extend(entries)
        retained_units = sum(record.units for record in result.retained)
        undated_units = sum(tally.undated_by_jurisdiction.values())
        dated_units = sum(
            cell[STAGE_MEASURES.index("total")] for cell in tally.cells.values()
        )
        if dated_units + undated_units != retained_units:
            raise AssertionError(f"Series {stage} quarters do not reconcile")
        selected_units = sum(record.units for record in selected)
        removed_units = sum(record.units for record in result.removed)
        if retained_units + removed_units != selected_units:
            raise AssertionError(f"Series {stage} unit conservation failed")
        stage_stats[stage] = {
            "source_columns": {
                "date": activity.date_field,
                "total": activity.total_field,
            },
            "dedupe_pass": "carried" if activity.carried_pass else "snapshot",
            "selected_row_count": len(selected),
            "selected_units": selected_units,
            "retained_row_count": len(result.retained),
            "removed_row_count": len(result.removed),
            "removed_units": removed_units,
            "retained_units": retained_units,
            "dated_units": dated_units,
            "undated_units": undated_units,
            "undated_units_by_jurisdiction": dict(
                sorted(
                    (key, units)
                    for key, units in tally.undated_by_jurisdiction.items()
                    if units
                )
            ),
            "removed_by_rule": _removal_rule_stats(result),
            "program_label_rows": dict(sorted(tally.program_label_rows.items())),
            "unrecognized_program_label_rows": dict(
                sorted(
                    (label, rows)
                    for label, rows in tally.program_label_rows.items()
                    if label not in PROGRAM_LABEL_MEASURES
                    and label not in SKIPPED_PROGRAM_LABELS
                )
            ),
            "unrecognized_unit_category_rows": dict(
                sorted(tally.unrecognized_unit_categories.items())
            ),
            "density_bonus_unparsed_rows": tally.density_bonus_unparsed_rows,
            "selection": {
                key: value
                for key, value in selection_stats.items()
                if key != "raw_jurisdictions_without_planning_period"
            },
        }

    totals = json.loads(totals_path.read_text(encoding="utf-8"))
    sync_blockers, reconciliation = _reconcile(totals, metadata, window, tallies)

    rows: list[list[object]] = []
    zero = [0] * len(STAGE_MEASURES)
    for key, period in sorted(
        window.items(), key=lambda item: item[1].display.casefold()
    ):
        for year in range(SERIES_START.year, cutoff_year + 1):
            filed_rows = report_year_counts.get((key, year), 0)
            flags: set[str] = set()
            if filed_rows == 0:
                flags.add("no_apr_rows")
            for singular, heaped_years in heaped.items():
                if (key, year) in heaped_years:
                    flags.add(f"{singular}_dates_heaped")
            if year == cutoff_year:
                flags.add("provisional_year")
            data_flags = ", ".join(flag for flag in DATA_FLAG_ORDER if flag in flags)
            for quarter in range(1, 5):
                values: list[object] = [
                    f"{period.display} {year} Q{quarter}",
                    period.display,
                    key,
                    year,
                    quarter,
                    dt.date(year, 3 * quarter - 2, 1).isoformat(),
                    filed_rows,
                    data_flags,
                ]
                for stage, _singular, _activity in SERIES_STAGES:
                    values.extend(tallies[stage].cells.get((key, year, quarter), zero))
                rows.append(values)

    metadata.update(
        {
            "jurisdiction_count": len(window),
            "row_count": len(rows),
            "quarters_per_jurisdiction": 4 * (cutoff_year - SERIES_START.year + 1),
            "provisional_years": [
                {
                    "year": cutoff_year,
                    "reason": (
                        "Latest report year: some jurisdictions have not filed it, "
                        "and HCD revises recent years as late reports arrive."
                    ),
                }
            ],
            "heaping_rule": {
                "min_dated_rows": HEAPING_MIN_ROWS,
                "min_share_on_one_date": HEAPING_MIN_SHARE,
            },
            "heaping": heaping_entries,
            "program_columns": {
                "field": PROGRAM_FIELD,
                "labels": dict(sorted(PROGRAM_LABEL_MEASURES.items())),
                "skipped_labels": sorted(SKIPPED_PROGRAM_LABELS),
                "density_bonus_field": DENSITY_BONUS_FIELD,
            },
            "reconciliation": reconciliation,
            "stages": stage_stats,
            "sync_blockers": sync_blockers,
        }
    )
    payload = {"columns": list(SERIES_COLUMNS), "metadata": metadata, "rows": rows}
    validate_json_document(payload, series_schema_path)
    series_json = output_dir / "airtable_series.json"
    _atomic_write(series_json, _series_text(payload))

    def units(stage: str) -> int:
        value = stage_stats[stage]["retained_units"]
        assert isinstance(value, int)
        return value

    return SeriesBuildArtifacts(
        series_json=series_json,
        jurisdiction_count=len(window),
        row_count=len(rows),
        sync_blocker_count=len(sync_blockers),
        entitlement_units=units("entitlements"),
        permit_units=units("permits"),
        completion_units=units("completions"),
    )


__all__ = [
    "SERIES_COLUMNS",
    "SERIES_STAGES",
    "STAGE_MEASURES",
    "SeriesBuildArtifacts",
    "build_series_artifacts",
]
