"""Build deterministic, read only Airtable comparison plans.

This module has no HTTP client and no apply function. Callers provide a complete,
projected Airtable snapshot. The plan compares only the four permit bands used by
the dashboard and binds the result to the exact source, snapshot, and mapping.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Mapping
from typing import Any

from .webflow import (
    canonical_json_bytes,
    canonical_sha256,
    validate_jurisdiction_totals,
)

AIRTABLE_TOTAL_FIELDS = ("vli", "li", "mi", "ami")
SNAPSHOT_ROOT_FIELDS = frozenset(
    {
        "snapshot_version",
        "complete",
        "retrieved_at",
        "base_id",
        "jurisdictions_table_id",
        "rhna_table_id",
        "record_counts",
        "jurisdictions",
        "rhna_records",
    }
)
JURISDICTION_RECORD_FIELDS = frozenset({"record_id", "name", "unincorporated"})
RHNA_RECORD_FIELDS = frozenset(
    {
        "record_id",
        "jurisdiction_record_ids",
        "cycle",
        "current",
        "correct_link",
        "vli",
        "li",
        "mi",
        "ami",
        "rhna_start",
        "rhna_end",
        "total_progress_override",
    }
)
MAPPING_FIELDS = frozenset(
    {
        "base_id",
        "jurisdictions_table_id",
        "rhna_table_id",
        "comparison_cycle",
        "excluded_jurisdictions",
        "jurisdiction_mappings",
    }
)
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_PLACEHOLDER_PREFIXES = ("replace-", "your-")


class AirtablePlanError(ValueError):
    """Base class for Airtable comparison validation errors."""


class AirtableSnapshotError(AirtablePlanError):
    """The projected Airtable snapshot is incomplete or ambiguous."""


class AirtableMappingError(AirtablePlanError):
    """The Airtable mapping is missing, stale, or ambiguous."""


class AirtableCoverageError(AirtablePlanError):
    """The selected cycle does not cover every expected jurisdiction once."""


class AirtableStateChangedError(AirtablePlanError):
    """The Airtable snapshot changed after the plan was reviewed."""


def _snapshot_keys(
    value: Mapping[str, object], expected: frozenset[str], label: str
) -> None:
    missing = sorted(expected - value.keys())
    if missing:
        raise AirtableSnapshotError(f"{label} is missing: " + ", ".join(missing))
    extra = sorted(value.keys() - expected)
    if extra:
        raise AirtableSnapshotError(
            f"{label} has unexpected fields: " + ", ".join(extra)
        )


def _nonempty_string(value: object, label: str, *, mapping: bool = False) -> str:
    error_type = AirtableMappingError if mapping else AirtableSnapshotError
    if not isinstance(value, str) or not value.strip():
        raise error_type(f"{label} must be a nonempty string")
    result = value.strip()
    if mapping and result.lower().startswith(_PLACEHOLDER_PREFIXES):
        raise error_type(f"{label} is still a placeholder")
    return result


def _nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AirtableSnapshotError(f"{label} must be a nonnegative integer")
    return value


def _optional_date(value: object, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise AirtableSnapshotError(f"{label} must be an ISO date or null")
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError as error:
        raise AirtableSnapshotError(f"{label} must be an ISO date or null") from error
    if parsed.isoformat() != value:
        raise AirtableSnapshotError(f"{label} must be an ISO date or null")
    return value


def _retrieved_at(value: object) -> str:
    result = _nonempty_string(value, "snapshot.retrieved_at")
    try:
        parsed = dt.datetime.fromisoformat(result)
    except ValueError as error:
        raise AirtableSnapshotError(
            "snapshot.retrieved_at must be an ISO timestamp with a time zone"
        ) from error
    if parsed.tzinfo is None:
        raise AirtableSnapshotError(
            "snapshot.retrieved_at must be an ISO timestamp with a time zone"
        )
    return result


def _normalize_snapshot(
    snapshot: object,
) -> tuple[
    dict[str, object],
    dict[str, dict[str, object]],
    list[dict[str, object]],
]:
    if not isinstance(snapshot, Mapping):
        raise AirtableSnapshotError("Airtable snapshot must be an object")
    raw = dict(snapshot)
    _snapshot_keys(raw, SNAPSHOT_ROOT_FIELDS, "snapshot")
    if raw["snapshot_version"] != 1:
        raise AirtableSnapshotError("snapshot.snapshot_version must be 1")
    if raw["complete"] is not True:
        raise AirtableSnapshotError("snapshot.complete must be true")

    normalized: dict[str, object] = {
        "snapshot_version": 1,
        "complete": True,
        "retrieved_at": _retrieved_at(raw["retrieved_at"]),
        "base_id": _nonempty_string(raw["base_id"], "snapshot.base_id"),
        "jurisdictions_table_id": _nonempty_string(
            raw["jurisdictions_table_id"], "snapshot.jurisdictions_table_id"
        ),
        "rhna_table_id": _nonempty_string(
            raw["rhna_table_id"], "snapshot.rhna_table_id"
        ),
    }

    counts_raw = raw["record_counts"]
    if not isinstance(counts_raw, Mapping):
        raise AirtableSnapshotError("snapshot.record_counts must be an object")
    if set(counts_raw) != {"jurisdictions", "rhna_records"}:
        raise AirtableSnapshotError(
            "snapshot.record_counts must contain jurisdictions and rhna_records"
        )
    record_counts = {
        "jurisdictions": _nonnegative_int(
            counts_raw["jurisdictions"], "record_counts.jurisdictions"
        ),
        "rhna_records": _nonnegative_int(
            counts_raw["rhna_records"], "record_counts.rhna_records"
        ),
    }

    jurisdictions_raw = raw["jurisdictions"]
    if not isinstance(jurisdictions_raw, list):
        raise AirtableSnapshotError("snapshot.jurisdictions must be an array")
    jurisdictions: list[dict[str, object]] = []
    by_id: dict[str, dict[str, object]] = {}
    names: set[str] = set()
    for position, item_raw in enumerate(jurisdictions_raw):
        if not isinstance(item_raw, Mapping):
            raise AirtableSnapshotError(f"jurisdictions[{position}] must be an object")
        item = dict(item_raw)
        _snapshot_keys(item, JURISDICTION_RECORD_FIELDS, f"jurisdictions[{position}]")
        record_id = _nonempty_string(
            item["record_id"], f"jurisdictions[{position}].record_id"
        )
        name = _nonempty_string(item["name"], f"jurisdictions[{position}].name")
        unincorporated = item["unincorporated"]
        if not isinstance(unincorporated, bool):
            raise AirtableSnapshotError(
                f"jurisdictions[{position}].unincorporated must be a boolean"
            )
        if record_id in by_id:
            raise AirtableSnapshotError(
                f"Duplicate Airtable jurisdiction record ID: {record_id!r}"
            )
        if name in names:
            raise AirtableSnapshotError(
                f"Duplicate Airtable jurisdiction name: {name!r}"
            )
        normalized_item = {
            "record_id": record_id,
            "name": name,
            "unincorporated": unincorporated,
        }
        jurisdictions.append(normalized_item)
        by_id[record_id] = normalized_item
        names.add(name)
    jurisdictions.sort(key=lambda item: str(item["record_id"]))

    rhna_raw = raw["rhna_records"]
    if not isinstance(rhna_raw, list):
        raise AirtableSnapshotError("snapshot.rhna_records must be an array")
    rhna_records: list[dict[str, object]] = []
    rhna_ids: set[str] = set()
    for position, item_raw in enumerate(rhna_raw):
        if not isinstance(item_raw, Mapping):
            raise AirtableSnapshotError(f"rhna_records[{position}] must be an object")
        item = dict(item_raw)
        _snapshot_keys(item, RHNA_RECORD_FIELDS, f"rhna_records[{position}]")
        record_id = _nonempty_string(
            item["record_id"], f"rhna_records[{position}].record_id"
        )
        if record_id in rhna_ids:
            raise AirtableSnapshotError(
                f"Duplicate Airtable RHNA record ID: {record_id!r}"
            )
        rhna_ids.add(record_id)
        links_raw = item["jurisdiction_record_ids"]
        if not isinstance(links_raw, list) or any(
            not isinstance(link, str) or not link.strip() for link in links_raw
        ):
            raise AirtableSnapshotError(
                f"rhna_records[{position}].jurisdiction_record_ids must be strings"
            )
        links = sorted(link.strip() for link in links_raw)
        current = item["current"]
        correct_link = item["correct_link"]
        if not isinstance(current, bool):
            raise AirtableSnapshotError(
                f"rhna_records[{position}].current must be a boolean"
            )
        if not isinstance(correct_link, bool):
            raise AirtableSnapshotError(
                f"rhna_records[{position}].correct_link must be a boolean"
            )
        override = item["total_progress_override"]
        if override is not None:
            override = _nonnegative_int(
                override, f"rhna_records[{position}].total_progress_override"
            )
        normalized_item = {
            "record_id": record_id,
            "jurisdiction_record_ids": links,
            "cycle": _nonempty_string(item["cycle"], f"rhna_records[{position}].cycle"),
            "current": current,
            "correct_link": correct_link,
            **{
                field: _nonnegative_int(
                    item[field], f"rhna_records[{position}].{field}"
                )
                for field in AIRTABLE_TOTAL_FIELDS
            },
            "rhna_start": _optional_date(
                item["rhna_start"], f"rhna_records[{position}].rhna_start"
            ),
            "rhna_end": _optional_date(
                item["rhna_end"], f"rhna_records[{position}].rhna_end"
            ),
            "total_progress_override": override,
        }
        canonical_json_bytes(normalized_item)
        rhna_records.append(normalized_item)
    rhna_records.sort(key=lambda item: str(item["record_id"]))

    if record_counts["jurisdictions"] != len(jurisdictions):
        raise AirtableSnapshotError(
            "record_counts.jurisdictions does not match the jurisdiction array"
        )
    if record_counts["rhna_records"] != len(rhna_records):
        raise AirtableSnapshotError(
            "record_counts.rhna_records does not match the RHNA array"
        )
    normalized["record_counts"] = record_counts
    normalized["jurisdictions"] = jurisdictions
    normalized["rhna_records"] = rhna_records
    canonical_json_bytes(normalized)
    return normalized, by_id, rhna_records


def _normalize_mapping(config: Mapping[str, object]) -> dict[str, object]:
    raw = dict(config)
    missing = sorted(MAPPING_FIELDS - raw.keys())
    if missing:
        raise AirtableMappingError("mapping is missing: " + ", ".join(missing))
    extra = sorted(raw.keys() - MAPPING_FIELDS)
    if extra:
        raise AirtableMappingError("mapping has unexpected fields: " + ", ".join(extra))
    normalized: dict[str, object] = {
        key: _nonempty_string(raw[key], f"mapping.{key}", mapping=True)
        for key in (
            "base_id",
            "jurisdictions_table_id",
            "rhna_table_id",
            "comparison_cycle",
        )
    }
    excluded_raw = raw["excluded_jurisdictions"]
    if not isinstance(excluded_raw, list):
        raise AirtableMappingError("mapping.excluded_jurisdictions must be an array")
    excluded: list[str] = []
    for position, value in enumerate(excluded_raw):
        name = _nonempty_string(
            value, f"mapping.excluded_jurisdictions[{position}]", mapping=True
        )
        if name in excluded:
            raise AirtableMappingError(f"Duplicate excluded jurisdiction: {name!r}")
        excluded.append(name)
    normalized["excluded_jurisdictions"] = sorted(excluded)

    mappings_raw = raw["jurisdiction_mappings"]
    if not isinstance(mappings_raw, Mapping):
        raise AirtableMappingError("mapping.jurisdiction_mappings must be an object")
    mappings: dict[str, str] = {}
    for source_raw, target_raw in mappings_raw.items():
        source = _nonempty_string(source_raw, "mapping jurisdiction name", mapping=True)
        target = _nonempty_string(
            target_raw, f"mapping.jurisdiction_mappings.{source}", mapping=True
        )
        mappings[source] = target
    normalized["jurisdiction_mappings"] = dict(sorted(mappings.items()))
    canonical_json_bytes(normalized)
    return normalized


def airtable_snapshot_sha256(snapshot: object) -> str:
    """Digest the projected Airtable state, ignoring only retrieval time."""

    normalized, _, _ = _normalize_snapshot(snapshot)
    digest_input = dict(normalized)
    digest_input.pop("retrieved_at", None)
    return canonical_sha256(digest_input)


def _target_bands(row: Mapping[str, object]) -> dict[str, int]:
    result = {
        "vli": sum(
            int(row[field]) for field in ("acutely_low", "extremely_low", "very_low")
        ),
        "li": int(row["low"]),
        "mi": int(row["moderate"]),
        "ami": int(row["above_moderate"]),
    }
    if sum(result.values()) != int(row["total"]):
        raise AirtablePlanError(
            f"Permit band mapping does not reconcile for {row['jurisdiction']!r}"
        )
    return result


def _one_link(
    row: Mapping[str, object],
    *,
    label: str,
    error_type: type[AirtablePlanError],
) -> str:
    links = row["jurisdiction_record_ids"]
    assert isinstance(links, list)
    if len(links) != 1:
        raise error_type(
            f"{label} must link to exactly one jurisdiction, found {len(links)}"
        )
    return str(links[0])


def build_airtable_change_plan(
    jurisdiction_totals: Mapping[str, object],
    airtable_snapshot: object,
    mapping_config: Mapping[str, object],
) -> dict[str, object]:
    """Compare reviewed permit totals with one exact Airtable cycle."""

    if not isinstance(jurisdiction_totals, Mapping):
        raise AirtablePlanError("jurisdiction_totals must be an object")
    if not isinstance(mapping_config, Mapping):
        raise AirtableMappingError("mapping_config must be an object")
    metadata, jurisdictions = validate_jurisdiction_totals(jurisdiction_totals)
    snapshot, airtable_by_id, rhna_records = _normalize_snapshot(airtable_snapshot)
    config = _normalize_mapping(mapping_config)

    for identifier in ("base_id", "jurisdictions_table_id", "rhna_table_id"):
        if config[identifier] != snapshot[identifier]:
            raise AirtableMappingError(
                f"mapping.{identifier} does not match the Airtable snapshot"
            )

    local_by_name = {str(row["jurisdiction"]): row for row in jurisdictions}
    airtable_by_name = {str(row["name"]): row for row in airtable_by_id.values()}
    excluded = set(config["excluded_jurisdictions"])
    missing_exclusions = sorted(excluded - airtable_by_name.keys())
    if missing_exclusions:
        raise AirtableMappingError(
            "Excluded Airtable jurisdictions are absent: "
            + ", ".join(missing_exclusions)
        )

    aliases = config["jurisdiction_mappings"]
    assert isinstance(aliases, Mapping)
    unknown_aliases = sorted(set(aliases) - local_by_name.keys())
    if unknown_aliases:
        raise AirtableMappingError(
            "Mappings refer to unknown local jurisdictions: "
            + ", ".join(unknown_aliases)
        )
    targets: dict[str, dict[str, object]] = {}
    target_names: dict[str, str] = {}
    for local_name in sorted(local_by_name):
        airtable_name = str(aliases.get(local_name, local_name))
        target = airtable_by_name.get(airtable_name)
        if target is None:
            raise AirtableMappingError(
                f"No exact Airtable jurisdiction matches {local_name!r}; "
                "add an explicit jurisdiction mapping"
            )
        if airtable_name in excluded:
            raise AirtableMappingError(
                f"Local jurisdiction {local_name!r} maps to excluded Airtable "
                f"jurisdiction {airtable_name!r}"
            )
        previous = target_names.get(airtable_name)
        if previous is not None:
            raise AirtableMappingError(
                f"Local jurisdictions {previous!r} and {local_name!r} both map "
                f"to {airtable_name!r}"
            )
        target_names[airtable_name] = local_name
        targets[local_name] = target

    unexpected_airtable = sorted(set(airtable_by_name) - set(target_names) - excluded)
    if unexpected_airtable:
        raise AirtableCoverageError(
            "Airtable has unexpected jurisdictions: " + ", ".join(unexpected_airtable)
        )

    comparison_cycle = str(config["comparison_cycle"])
    selected_by_jurisdiction_id: dict[str, dict[str, object]] = {}
    selected_overrides: list[str] = []
    for row in rhna_records:
        if row["cycle"] != comparison_cycle:
            continue
        jurisdiction_id = _one_link(
            row,
            label=f"RHNA record {row['record_id']!r}",
            error_type=AirtableCoverageError,
        )
        jurisdiction = airtable_by_id.get(jurisdiction_id)
        if jurisdiction is None:
            raise AirtableCoverageError(
                f"RHNA record {row['record_id']!r} links to an unknown jurisdiction"
            )
        if jurisdiction["name"] in excluded:
            continue
        if not row["correct_link"]:
            raise AirtableCoverageError(
                f"RHNA record {row['record_id']!r} fails Airtable Correct Link?"
            )
        if jurisdiction_id in selected_by_jurisdiction_id:
            raise AirtableCoverageError(
                f"Cycle {comparison_cycle!r} has duplicate RHNA rows for "
                f"{jurisdiction['name']!r}"
            )
        selected_by_jurisdiction_id[jurisdiction_id] = row
        if row["total_progress_override"] not in (None, 0):
            selected_overrides.append(str(jurisdiction["name"]))

    changes: list[dict[str, object]] = []
    unchanged_count = 0
    before_aggregate = {field: 0 for field in AIRTABLE_TOTAL_FIELDS}
    after_aggregate = {field: 0 for field in AIRTABLE_TOTAL_FIELDS}
    for local_name in sorted(local_by_name):
        source_row = local_by_name[local_name]
        target = targets[local_name]
        rhna_row = selected_by_jurisdiction_id.get(str(target["record_id"]))
        if rhna_row is None:
            raise AirtableCoverageError(
                f"Cycle {comparison_cycle!r} has no RHNA row for {local_name!r}"
            )
        before = {field: int(rhna_row[field]) for field in AIRTABLE_TOTAL_FIELDS}
        after = _target_bands(source_row)
        for field in AIRTABLE_TOTAL_FIELDS:
            before_aggregate[field] += before[field]
            after_aggregate[field] += after[field]
        changed_fields = [
            field for field in AIRTABLE_TOTAL_FIELDS if before[field] != after[field]
        ]
        if not changed_fields:
            unchanged_count += 1
            continue
        fields = {
            field: {
                "before": before[field],
                "after": after[field],
                "delta": after[field] - before[field],
            }
            for field in AIRTABLE_TOTAL_FIELDS
        }
        changes.append(
            {
                "airtable_record_id": rhna_row["record_id"],
                "jurisdiction_record_id": target["record_id"],
                "jurisdiction": local_name,
                "airtable_jurisdiction": target["name"],
                "cycle": comparison_cycle,
                "changed_fields": changed_fields,
                "fields": fields,
                "before_total": sum(before.values()),
                "after_total": sum(after.values()),
                "delta_total": sum(after.values()) - sum(before.values()),
                "unmapped_quality": {
                    "data_status": source_row["data_status"],
                    "undated_permits": source_row["undated_permits"],
                },
            }
        )

    current_rows_by_jurisdiction: dict[str, list[dict[str, object]]] = {}
    malformed_current_links: list[str] = []
    for row in rhna_records:
        if not row["current"]:
            continue
        links = row["jurisdiction_record_ids"]
        assert isinstance(links, list)
        if len(links) != 1 or str(links[0]) not in airtable_by_id:
            malformed_current_links.append(str(row["record_id"]))
            continue
        jurisdiction_id = str(links[0])
        if airtable_by_id[jurisdiction_id]["name"] in excluded:
            continue
        current_rows_by_jurisdiction.setdefault(jurisdiction_id, []).append(row)

    current_cycle_counts: dict[str, int] = {}
    current_other_cycle_names: list[str] = []
    current_duplicate_names: list[str] = []
    missing_current_names: list[str] = []
    for local_name in sorted(local_by_name):
        jurisdiction_id = str(targets[local_name]["record_id"])
        rows = current_rows_by_jurisdiction.get(jurisdiction_id, [])
        if not rows:
            missing_current_names.append(local_name)
            continue
        if len(rows) > 1:
            current_duplicate_names.append(local_name)
        for row in rows:
            cycle = str(row["cycle"])
            current_cycle_counts[cycle] = current_cycle_counts.get(cycle, 0) + 1
            if cycle != comparison_cycle:
                current_other_cycle_names.append(local_name)

    blockers: list[dict[str, object]] = [
        {
            "code": "source_cycle_not_declared",
            "message": (
                "The jurisdiction totals schema does not declare a planning cycle, "
                "so this comparison cannot authorize an Airtable update."
            ),
        }
    ]
    if current_other_cycle_names:
        blockers.append(
            {
                "code": "airtable_current_scope_uses_other_cycles",
                "message": (
                    "Airtable marks jurisdictions outside the comparison cycle as current."
                ),
                "jurisdictions": sorted(set(current_other_cycle_names)),
            }
        )
    if missing_current_names:
        blockers.append(
            {
                "code": "airtable_current_scope_missing_jurisdictions",
                "message": "Airtable has no current RHNA row for these jurisdictions.",
                "jurisdictions": missing_current_names,
            }
        )
    if current_duplicate_names:
        blockers.append(
            {
                "code": "airtable_current_scope_duplicate_jurisdictions",
                "message": "Airtable has more than one current RHNA row for these jurisdictions.",
                "jurisdictions": current_duplicate_names,
            }
        )
    if malformed_current_links:
        blockers.append(
            {
                "code": "airtable_current_scope_invalid_links",
                "message": "Some current RHNA rows do not link to exactly one known jurisdiction.",
                "record_ids": sorted(malformed_current_links),
            }
        )
    if selected_overrides:
        blockers.append(
            {
                "code": "airtable_progress_overrides_present",
                "message": (
                    "Airtable progress overrides would prevent the four permit bands "
                    "from controlling displayed progress."
                ),
                "jurisdictions": sorted(selected_overrides),
            }
        )

    blocked_names = set(current_other_cycle_names) | set(missing_current_names)
    blocked_names.update(current_duplicate_names)
    changes.sort(key=lambda item: str(item["jurisdiction"]))
    plan: dict[str, Any] = {
        "plan_version": 1,
        "intent": "comparison_only",
        "apply_eligible": False,
        "comparison_cycle": comparison_cycle,
        "source_manifest_sha256": metadata["source_manifest_sha256"],
        "dedupe_profile": metadata["dedupe_profile"],
        "cutoff_year": metadata["cutoff_year"],
        "source_last_updated": metadata["last_updated"],
        "airtable_retrieved_at": snapshot["retrieved_at"],
        "airtable_snapshot_sha256": airtable_snapshot_sha256(snapshot),
        "mapping_sha256": canonical_sha256(config),
        "target": {
            "base_id": config["base_id"],
            "jurisdictions_table_id": config["jurisdictions_table_id"],
            "rhna_table_id": config["rhna_table_id"],
        },
        "matched_count": len(jurisdictions),
        "change_count": len(changes),
        "unchanged_count": unchanged_count,
        "current_scope": {
            "record_count": sum(current_cycle_counts.values()),
            "cycle_counts": dict(sorted(current_cycle_counts.items())),
            "blocked_jurisdiction_count": len(blocked_names),
            "missing_jurisdictions": missing_current_names,
            "other_cycle_jurisdictions": sorted(set(current_other_cycle_names)),
            "duplicate_jurisdictions": current_duplicate_names,
        },
        "airtable_aggregate_before": {
            **before_aggregate,
            "total": sum(before_aggregate.values()),
        },
        "source_aggregate_after": {
            **after_aggregate,
            "total": sum(after_aggregate.values()),
        },
        "unmapped_source_fields": ["data_status", "undated_permits"],
        "limitations": [
            "Airtable cycle dates are reported for review only and are not counting windows.",
            "The Airtable Current RHNA? formula is reported for review and is not authoritative.",
        ],
        "blockers": blockers,
        "changes": changes,
    }
    plan["plan_sha256"] = canonical_sha256(plan)
    return plan


def verify_airtable_change_plan(
    plan: Mapping[str, object], current_airtable_snapshot: object
) -> None:
    """Fail unless the plan is intact and the complete Airtable state is unchanged."""

    expected_plan_digest = plan.get("plan_sha256")
    if not isinstance(expected_plan_digest, str) or not _SHA256_RE.fullmatch(
        expected_plan_digest
    ):
        raise AirtablePlanError("Plan has no valid plan_sha256")
    digest_input = dict(plan)
    digest_input.pop("plan_sha256", None)
    if canonical_sha256(digest_input) != expected_plan_digest:
        raise AirtablePlanError("Plan content does not match plan_sha256")
    expected_snapshot_digest = plan.get("airtable_snapshot_sha256")
    if not isinstance(expected_snapshot_digest, str) or not _SHA256_RE.fullmatch(
        expected_snapshot_digest
    ):
        raise AirtablePlanError("Plan has no valid airtable_snapshot_sha256")
    if airtable_snapshot_sha256(current_airtable_snapshot) != expected_snapshot_digest:
        raise AirtableStateChangedError(
            "Airtable state changed after the plan was built; rebuild and review the plan"
        )
