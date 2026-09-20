"""Compare submitted Table B totals with the current permit counter.

visibility: public

This report never fills missing Table Bs from Table A2 or the blank HCD template.
It has no publication path: incomplete coverage and unexplained differences stay
visible while the source collection is assembled.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections.abc import Mapping
from pathlib import Path

from .airtable_sync import _validate_source
from .normalize import normalized_key
from .progress import calculate_progress
from .table_b import read_table_b_workbook
from .webflow import canonical_sha256

BANDS = ("vli", "li", "mi", "ami")


def _bands(row: Mapping[str, object]) -> dict[str, int]:
    return {
        "vli": sum(
            int(row[name]) for name in ("acutely_low", "extremely_low", "very_low")
        ),
        "li": int(row["low"]),
        "mi": int(row["moderate"]),
        "ami": int(row["above_moderate"]),
    }


def _reviewed_pdf(entry: dict[str, object], path: Path) -> dict[str, object]:
    """Read an explicitly transcribed PDF table with file and page provenance."""
    if type(entry.get("page")) is not int or entry["page"] < 1:
        raise ValueError("A transcribed Table B must identify its PDF page")
    row = dict(entry["values"])
    for field in ("permit_values", "allocation_values", "projection_credit_by_income"):
        values = row.get(field)
        if not isinstance(values, dict) or set(values) != set(BANDS):
            raise ValueError(f"{field} must contain exactly VLI, LI, MI, and AMI")
        if any(type(value) is not int or value < 0 for value in values.values()):
            raise ValueError(f"{field} must contain explicit nonnegative integers")
    for field in ("total", "total_allocation", "projection_credit"):
        if type(row.get(field)) is not int or row[field] < 0:
            raise ValueError(f"{field} must be an explicit nonnegative integer")
    if sum(row["permit_values"].values()) != row["total"]:
        raise ValueError("Table B permit total does not reconcile")
    if sum(row["allocation_values"].values()) != row["total_allocation"]:
        raise ValueError("Table B allocation total does not reconcile")
    if sum(row["projection_credit_by_income"].values()) != row["projection_credit"]:
        raise ValueError("Table B projection credit does not reconcile")
    row["source_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    row["source_cells"] = {"page": entry["page"]}
    return row


def build_table_b_review(
    *,
    baseline: Mapping[str, object],
    manifest_path: Path,
    cycle_policy: Mapping[str, object],
) -> dict[str, object]:
    """Build a complete coverage report without generating publication values."""
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("manifest_version") != 1 or not isinstance(
        manifest.get("sources"), list
    ):
        raise ValueError("Expected Table B manifest version 1 and sources array")
    metadata, old_rows = _validate_source(baseline)
    if not old_rows or len(old_rows) != cycle_policy.get("expected_jurisdiction_count"):
        raise ValueError(
            "Baseline does not have the complete expected jurisdiction coverage"
        )
    if metadata["cycle_policy_sha256"] != canonical_sha256(dict(cycle_policy)):
        raise ValueError("Baseline does not match the selected cycle policy")
    if metadata["cycle_counts"] != cycle_policy.get("expected_cycle_counts"):
        raise ValueError(
            "Baseline cycle counts disagree with the selected cycle policy"
        )
    cutoff_year = metadata["cutoff_year"]
    if type(cutoff_year) is not int or manifest.get("reporting_year") != cutoff_year:
        raise ValueError(
            "Table B manifest and existing artifact must use the same reporting year"
        )
    by_key = {row["jurisdiction_key"]: row for row in old_rows}
    if len(by_key) != len(old_rows):
        raise ValueError("Existing artifact has duplicate jurisdiction keys")
    sources: dict[str, dict[str, object]] = {}
    for entry in manifest["sources"]:
        key = entry.get("jurisdiction_key")
        if not isinstance(key, str) or normalized_key(key) != key or key not in by_key:
            raise ValueError(f"Source has an unexpected jurisdiction key: {key!r}")
        if key in sources:
            raise ValueError(f"Multiple cumulative Table Bs selected for {key}")
        sources[key] = entry

    comparisons = []
    missing = []
    errors = []
    unexplained = []
    for key, old in sorted(by_key.items()):
        entry = sources.get(key)
        if entry is None:
            missing.append(key)
            continue
        try:
            path = (manifest_path.parent / str(entry["path"])).resolve()
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != entry.get("sha256"):
                raise ValueError("Source bytes do not match the pinned SHA-256")
            url = entry.get("url")
            if not isinstance(url, str) or not url.startswith("https://"):
                raise ValueError("Source needs its official HTTPS URL")
            if entry.get("format") == "reviewed_pdf":
                new = _reviewed_pdf(entry, path)
            elif entry.get("format") == "hcd_workbook":
                new = read_table_b_workbook(
                    path,
                    expected_jurisdiction_key=key,
                    expected_cycle=old["cycle"],
                    expected_reporting_year=cutoff_year,
                )
            else:
                raise ValueError("Unsupported Table B source format")
            expected = {
                "jurisdiction_key": key,
                "cycle": old["cycle"],
                "reporting_year": cutoff_year,
                "data_through": f"{cutoff_year}-12-31",
                "period_start": old["period_start"],
                "period_end": old["period_end"],
            }
            for field, value in expected.items():
                if new.get(field) != value:
                    raise ValueError(
                        f"Table B {field} disagrees with the selected cycle/report: {new.get(field)!r} != {value!r}"
                    )
            before = _bands(old)
            after = new["permit_values"]
            residual = new["total"] - old["total"] - new["projection_credit"]
            residual_by_income = {
                band: after[band]
                - before[band]
                - new["projection_credit_by_income"][band]
                for band in BANDS
            }
            needs_reconciliation = any(residual_by_income.values())
            if needs_reconciliation:
                unexplained.append(key)
            pace = calculate_progress(
                credited_units=new["total"],
                allocation=new["total_allocation"],
                planning_start=dt.date.fromisoformat(new["period_start"]),
                planning_end=dt.date.fromisoformat(new["period_end"]),
                data_through=dt.date.fromisoformat(new["data_through"]),
            )
            comparisons.append(
                {
                    **expected,
                    "before": before,
                    "table_b": after,
                    "difference": {band: after[band] - before[band] for band in BANDS},
                    "before_total": old["total"],
                    "table_b_total": new["total"],
                    "total_difference": new["total"] - old["total"],
                    "projection_credit": new["projection_credit"],
                    "projection_credit_by_income": new["projection_credit_by_income"],
                    "difference_beyond_projection_credit": residual,
                    "difference_beyond_projection_by_income": residual_by_income,
                    "needs_reconciliation": needs_reconciliation,
                    "allocation_values": new["allocation_values"],
                    "total_allocation": new["total_allocation"],
                    "pace": pace,
                    "pace_by_income": {
                        band: calculate_progress(
                            credited_units=after[band],
                            allocation=new["allocation_values"][band],
                            planning_start=dt.date.fromisoformat(new["period_start"]),
                            planning_end=dt.date.fromisoformat(new["period_end"]),
                            data_through=dt.date.fromisoformat(new["data_through"]),
                        )
                        for band in BANDS
                    },
                    "source_url": url,
                    "source_sha256": digest,
                    "source_cells": new["source_cells"],
                }
            )
        except (OSError, ValueError, KeyError, TypeError) as error:
            errors.append({"jurisdiction_key": key, "error": str(error)})
    return {
        "report_version": 1,
        "visibility": "public",
        "purpose": "Review of submitted Table B totals; not a publication artifact",
        "reporting_year": cutoff_year,
        "data_through": f"{cutoff_year}-12-31",
        "expected_jurisdiction_count": len(by_key),
        "compared_jurisdiction_count": len(comparisons),
        "missing_jurisdictions": missing,
        "source_errors": errors,
        "unexplained_differences": unexplained,
        "coverage_complete": not missing and not errors,
        "ready_for_publication_review": not missing and not errors and not unexplained,
        "comparisons": comparisons,
    }
