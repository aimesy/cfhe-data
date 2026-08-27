"""Build deterministic, reviewable Webflow CMS change plans.

This module deliberately has no HTTP client and no apply function.  Callers must
provide a complete CMS snapshot.  A later publication step can use
``verify_change_plan`` to prove that the reviewed plan and the current snapshot
still agree before it performs any write.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

REQUIRED_TOTAL_FIELDS = (
    "acutely_low",
    "extremely_low",
    "very_low",
    "low",
    "moderate",
    "above_moderate",
    "total",
    "undated_permits",
)

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_PLACEHOLDER_PREFIXES = ("replace-", "your-")
METADATA_FIELDS = frozenset(
    {
        "cutoff_year",
        "last_updated",
        "provisional",
        "complete_after",
        "source_manifest_sha256",
        "dedupe_profile",
        "jurisdiction_count",
        "selected_row_count",
        "selected_units",
        "retained_row_count",
        "removed_row_count",
        "removed_units",
        "undated_permits",
    }
)
JURISDICTION_FIELDS = frozenset(
    {
        "jurisdiction",
        "jurisdiction_key",
        *REQUIRED_TOTAL_FIELDS,
        "last_updated",
        "data_status",
    }
)


class WebflowPlanError(ValueError):
    """Base class for change plan validation errors."""


class InvalidTotalsError(WebflowPlanError):
    """The jurisdiction totals object is not safe to publish."""


class MappingConfigurationError(WebflowPlanError):
    """The CMS mapping is missing or ambiguous."""


class CMSSnapshotError(WebflowPlanError):
    """The caller supplied CMS snapshot is incomplete or ambiguous."""


class UnknownJurisdictionError(CMSSnapshotError):
    """A jurisdiction exists on only one side of the proposed update."""


class CMSStateChangedError(CMSSnapshotError):
    """The CMS snapshot no longer matches the snapshot bound to a plan."""


def canonical_json_bytes(value: object) -> bytes:
    """Return the canonical UTF-8 JSON representation used for all digests."""

    try:
        rendered = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise WebflowPlanError(f"Value is not canonical JSON: {error}") from error
    return rendered.encode("utf-8")


def canonical_sha256(value: object) -> str:
    """Return a SHA-256 digest for canonical JSON data."""

    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _nonempty_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WebflowPlanError(f"{label} must be a nonempty string")
    result = value.strip()
    if result.lower().startswith(_PLACEHOLDER_PREFIXES):
        raise WebflowPlanError(f"{label} is still a placeholder")
    return result


def _nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise InvalidTotalsError(f"{label} must be a nonnegative integer")
    return value


def _date_string(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise InvalidTotalsError(f"{label} must be an ISO date")
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError as error:
        raise InvalidTotalsError(f"{label} must be an ISO date") from error
    if parsed.isoformat() != value:
        raise InvalidTotalsError(f"{label} must be an ISO date")
    return value


def validate_jurisdiction_totals(
    totals: Mapping[str, object],
) -> tuple[dict[str, object], list[dict[str, object]]]:
    metadata_raw = totals.get("metadata")
    jurisdictions_raw = totals.get("jurisdictions")
    if not isinstance(metadata_raw, Mapping):
        raise InvalidTotalsError("jurisdiction_totals.metadata must be an object")
    if not isinstance(jurisdictions_raw, list):
        raise InvalidTotalsError("jurisdiction_totals.jurisdictions must be an array")

    metadata = dict(metadata_raw)
    missing_metadata = sorted(METADATA_FIELDS - metadata.keys())
    if missing_metadata:
        raise InvalidTotalsError(
            "jurisdiction_totals.metadata is missing: " + ", ".join(missing_metadata)
        )
    extra_metadata = sorted(metadata.keys() - METADATA_FIELDS)
    if extra_metadata:
        raise InvalidTotalsError(
            "jurisdiction_totals.metadata has unexpected fields: "
            + ", ".join(extra_metadata)
        )

    cutoff_year = _nonnegative_int(metadata["cutoff_year"], "metadata.cutoff_year")
    if cutoff_year < 1900 or cutoff_year > 3000:
        raise InvalidTotalsError("metadata.cutoff_year is outside the supported range")
    source_digest = metadata["source_manifest_sha256"]
    if not isinstance(source_digest, str) or not _SHA256_RE.fullmatch(source_digest):
        raise InvalidTotalsError(
            "metadata.source_manifest_sha256 must be a lowercase SHA-256 digest"
        )
    if (
        not isinstance(metadata["dedupe_profile"], str)
        or not metadata["dedupe_profile"].strip()
    ):
        raise InvalidTotalsError("metadata.dedupe_profile must be a nonempty string")
    _date_string(metadata["last_updated"], "metadata.last_updated")
    _date_string(metadata["complete_after"], "metadata.complete_after")
    provisional = metadata["provisional"]
    if not isinstance(provisional, bool):
        raise InvalidTotalsError("metadata.provisional must be a boolean")
    jurisdiction_count = _nonnegative_int(
        metadata["jurisdiction_count"], "metadata.jurisdiction_count"
    )
    selected_row_count = _nonnegative_int(
        metadata["selected_row_count"], "metadata.selected_row_count"
    )
    selected_units = _nonnegative_int(
        metadata["selected_units"], "metadata.selected_units"
    )
    retained_row_count = _nonnegative_int(
        metadata["retained_row_count"], "metadata.retained_row_count"
    )
    removed_row_count = _nonnegative_int(
        metadata["removed_row_count"], "metadata.removed_row_count"
    )
    removed_units = _nonnegative_int(
        metadata["removed_units"], "metadata.removed_units"
    )
    undated_permits = _nonnegative_int(
        metadata["undated_permits"], "metadata.undated_permits"
    )
    if retained_row_count != selected_row_count - removed_row_count:
        raise InvalidTotalsError(
            "metadata row counts do not reconcile: retained must equal selected "
            "minus removed"
        )
    if removed_units > selected_units:
        raise InvalidTotalsError("metadata.removed_units exceeds selected_units")
    if jurisdiction_count != len(jurisdictions_raw):
        raise InvalidTotalsError(
            "metadata.jurisdiction_count does not match the jurisdiction array"
        )

    jurisdictions: list[dict[str, object]] = []
    seen_names: set[str] = set()
    for position, raw in enumerate(jurisdictions_raw):
        if not isinstance(raw, Mapping):
            raise InvalidTotalsError(f"jurisdictions[{position}] must be an object")
        row = dict(raw)
        missing_row_fields = sorted(JURISDICTION_FIELDS - row.keys())
        if missing_row_fields:
            raise InvalidTotalsError(
                f"jurisdictions[{position}] is missing: "
                + ", ".join(missing_row_fields)
            )
        extra_row_fields = sorted(row.keys() - JURISDICTION_FIELDS)
        if extra_row_fields:
            raise InvalidTotalsError(
                f"jurisdictions[{position}] has unexpected fields: "
                + ", ".join(extra_row_fields)
            )
        name = row.get("jurisdiction")
        if not isinstance(name, str) or not name.strip():
            raise InvalidTotalsError(
                f"jurisdictions[{position}].jurisdiction must be a nonempty string"
            )
        name = name.strip()
        if name in seen_names:
            raise InvalidTotalsError(f"Duplicate jurisdiction in totals: {name!r}")
        seen_names.add(name)
        row["jurisdiction"] = name
        jurisdiction_key = row["jurisdiction_key"]
        if not isinstance(jurisdiction_key, str) or not jurisdiction_key.strip():
            raise InvalidTotalsError(
                f"{name}.jurisdiction_key must be a nonempty string"
            )
        if row["last_updated"] != metadata["last_updated"]:
            raise InvalidTotalsError(
                f"{name}.last_updated does not match metadata.last_updated"
            )
        _date_string(row["last_updated"], f"{name}.last_updated")
        if row["data_status"] not in {"reported", "no_selected_rows"}:
            raise InvalidTotalsError(f"{name}.data_status is invalid")
        for field in REQUIRED_TOTAL_FIELDS:
            if field not in row:
                raise InvalidTotalsError(f"{name!r} is missing total field {field!r}")
            _nonnegative_int(row[field], f"{name}.{field}")
        category_total = sum(int(row[field]) for field in REQUIRED_TOTAL_FIELDS[:6])
        if row["total"] != category_total:
            raise InvalidTotalsError(
                f"{name}.total does not equal the six permit categories"
            )
        if int(row["undated_permits"]) > int(row["total"]):
            raise InvalidTotalsError(
                f"{name}.undated_permits cannot exceed total permits"
            )
        canonical_json_bytes(row)
        jurisdictions.append(row)

    retained_units = sum(int(row["total"]) for row in jurisdictions)
    if retained_units != selected_units - removed_units:
        raise InvalidTotalsError(
            "jurisdiction totals do not reconcile with selected_units minus "
            "removed_units"
        )
    if sum(int(row["undated_permits"]) for row in jurisdictions) != undated_permits:
        raise InvalidTotalsError(
            "jurisdiction undated permits do not reconcile with metadata"
        )

    canonical_json_bytes(metadata)
    jurisdictions.sort(key=lambda row: str(row["jurisdiction"]))
    return metadata, jurisdictions


def _normalize_mapping_config(
    config: Mapping[str, object], jurisdiction_names: set[str]
) -> dict[str, object]:
    try:
        match_field = _nonempty_string(
            config.get("jurisdiction_match_field"), "jurisdiction_match_field"
        )
        item_id_field = _nonempty_string(
            config.get("item_id_field", "id"), "item_id_field"
        )
        field_data_field = _nonempty_string(
            config.get("field_data_field", "fieldData"), "field_data_field"
        )
    except WebflowPlanError as error:
        raise MappingConfigurationError(str(error)) from error

    fields_raw = config.get("fields")
    if not isinstance(fields_raw, Mapping):
        raise MappingConfigurationError("fields must be an object")
    missing_fields = [
        field for field in REQUIRED_TOTAL_FIELDS if field not in fields_raw
    ]
    if missing_fields:
        raise MappingConfigurationError(
            "Missing required field mappings: " + ", ".join(missing_fields)
        )

    fields: dict[str, str] = {}
    used_cms_fields: dict[str, str] = {}
    for source_field, target_raw in fields_raw.items():
        if not isinstance(source_field, str) or not source_field.strip():
            raise MappingConfigurationError("Every source field name must be nonempty")
        source_field = source_field.strip()
        try:
            target_field = _nonempty_string(target_raw, f"fields.{source_field}")
        except WebflowPlanError as error:
            raise MappingConfigurationError(str(error)) from error
        if target_field == match_field:
            raise MappingConfigurationError(
                f"Field mapping {source_field!r} would overwrite the jurisdiction match field"
            )
        previous_source = used_cms_fields.get(target_field)
        if previous_source is not None:
            raise MappingConfigurationError(
                f"CMS field {target_field!r} is mapped from both "
                f"{previous_source!r} and {source_field!r}"
            )
        used_cms_fields[target_field] = source_field
        fields[source_field] = target_field

    mappings_raw = config.get("jurisdiction_mappings", {})
    if not isinstance(mappings_raw, Mapping):
        raise MappingConfigurationError("jurisdiction_mappings must be an object")
    unknown_mapping_keys = sorted(set(mappings_raw) - jurisdiction_names)
    if unknown_mapping_keys:
        raise MappingConfigurationError(
            "Mappings refer to unknown jurisdictions: "
            + ", ".join(unknown_mapping_keys)
        )

    jurisdiction_mappings: dict[str, dict[str, str]] = {}
    seen_targets: dict[tuple[str, str], str] = {}
    for jurisdiction in sorted(jurisdiction_names):
        raw = mappings_raw.get(jurisdiction, jurisdiction)
        if isinstance(raw, str):
            selector = {"match_value": raw.strip()}
        elif isinstance(raw, Mapping):
            selector_keys = set(raw) & {"item_id", "match_value"}
            if len(selector_keys) != 1:
                raise MappingConfigurationError(
                    f"Mapping for {jurisdiction!r} must contain exactly one of "
                    "item_id or match_value"
                )
            selector_key = selector_keys.pop()
            selector_value = raw[selector_key]
            if not isinstance(selector_value, str):
                raise MappingConfigurationError(
                    f"Mapping selector for {jurisdiction!r} must be a string"
                )
            selector = {selector_key: selector_value.strip()}
        else:
            raise MappingConfigurationError(
                f"Mapping for {jurisdiction!r} must be a string or object"
            )
        selector_key, selector_value = next(iter(selector.items()))
        if not selector_value:
            raise MappingConfigurationError(
                f"Mapping selector for {jurisdiction!r} must not be empty"
            )
        target = (selector_key, selector_value)
        previous_jurisdiction = seen_targets.get(target)
        if previous_jurisdiction is not None:
            raise MappingConfigurationError(
                f"Jurisdictions {previous_jurisdiction!r} and {jurisdiction!r} "
                f"both map to {selector_key} {selector_value!r}"
            )
        seen_targets[target] = jurisdiction
        jurisdiction_mappings[jurisdiction] = selector

    normalized: dict[str, object] = {
        "jurisdiction_match_field": match_field,
        "item_id_field": item_id_field,
        "field_data_field": field_data_field,
        "fields": dict(sorted(fields.items())),
        "jurisdiction_mappings": jurisdiction_mappings,
    }
    for identifier in ("site_id", "collection_id"):
        if identifier in config:
            try:
                normalized[identifier] = _nonempty_string(
                    config[identifier], identifier
                )
            except WebflowPlanError as error:
                raise MappingConfigurationError(str(error)) from error
    return normalized


def _normalize_snapshot(
    snapshot: object, *, item_id_field: str
) -> tuple[list[dict[str, object]], object]:
    if isinstance(snapshot, Mapping):
        if "items" not in snapshot:
            raise CMSSnapshotError("CMS snapshot object must contain an items array")
        raw_items = snapshot["items"]
        snapshot_is_object = True
    else:
        raw_items = snapshot
        snapshot_is_object = False
    if not isinstance(raw_items, Sequence) or isinstance(
        raw_items, (str, bytes, bytearray)
    ):
        raise CMSSnapshotError("CMS snapshot items must be an array")

    items: list[dict[str, object]] = []
    seen_ids: set[str] = set()
    for position, raw_item in enumerate(raw_items):
        if not isinstance(raw_item, Mapping):
            raise CMSSnapshotError(f"CMS item {position} must be an object")
        try:
            item = json.loads(canonical_json_bytes(dict(raw_item)))
        except WebflowPlanError as error:
            raise CMSSnapshotError(
                f"CMS item {position} is invalid: {error}"
            ) from error
        item_id = item.get(item_id_field)
        if not isinstance(item_id, str) or not item_id.strip():
            raise CMSSnapshotError(
                f"CMS item {position} has no nonempty {item_id_field!r}"
            )
        item_id = item_id.strip()
        if item_id in seen_ids:
            raise CMSSnapshotError(f"Duplicate CMS item ID: {item_id!r}")
        seen_ids.add(item_id)
        item[item_id_field] = item_id
        items.append(item)
    items.sort(key=lambda item: str(item[item_id_field]))

    if snapshot_is_object:
        try:
            normalized_snapshot = json.loads(canonical_json_bytes(dict(snapshot)))
        except WebflowPlanError as error:
            raise CMSSnapshotError(f"CMS snapshot is invalid: {error}") from error
        normalized_snapshot["items"] = items
    else:
        normalized_snapshot = items
    return items, normalized_snapshot


def cms_snapshot_sha256(snapshot: object, *, item_id_field: str = "id") -> str:
    """Digest a complete CMS snapshot, ignoring only item ordering."""

    _, normalized_snapshot = _normalize_snapshot(snapshot, item_id_field=item_id_field)
    return canonical_sha256(normalized_snapshot)


def _source_value(
    source_field: str,
    row: Mapping[str, object],
    metadata: Mapping[str, object],
) -> object:
    in_row = source_field in row and source_field != "jurisdiction"
    in_metadata = source_field in metadata
    if source_field in JURISDICTION_FIELDS and in_row:
        return row[source_field]
    if in_row and in_metadata and row[source_field] != metadata[source_field]:
        raise MappingConfigurationError(
            f"Configured source field {source_field!r} is ambiguous between a "
            "jurisdiction row and metadata"
        )
    if in_row:
        return row[source_field]
    if in_metadata:
        return metadata[source_field]
    if source_field == "last_updated":
        aliases = [
            metadata[key]
            for key in ("last_updated", "source_last_updated")
            if key in metadata
        ]
        if aliases and all(value == aliases[0] for value in aliases):
            return aliases[0]
    raise MappingConfigurationError(
        f"Configured source field {source_field!r} is absent from totals"
    )


def build_change_plan(
    jurisdiction_totals: Mapping[str, object],
    cms_snapshot: object,
    mapping_config: Mapping[str, object],
) -> dict[str, object]:
    """Create a deterministic plan without making a network call or CMS write.

    ``jurisdiction_mappings`` values may be an exact match-field string or an
    object containing exactly one of ``match_value`` or ``item_id``.  When no
    explicit mapping exists, the canonical jurisdiction name is matched exactly.
    No case folding, slug generation, fuzzy matching, or type coercion occurs.
    """

    if not isinstance(jurisdiction_totals, Mapping):
        raise InvalidTotalsError("jurisdiction_totals must be an object")
    if not isinstance(mapping_config, Mapping):
        raise MappingConfigurationError("mapping_config must be an object")
    metadata, jurisdictions = validate_jurisdiction_totals(jurisdiction_totals)
    jurisdiction_names = {str(row["jurisdiction"]) for row in jurisdictions}
    config = _normalize_mapping_config(mapping_config, jurisdiction_names)

    item_id_field = str(config["item_id_field"])
    field_data_field = str(config["field_data_field"])
    match_field = str(config["jurisdiction_match_field"])
    items, normalized_snapshot = _normalize_snapshot(
        cms_snapshot, item_id_field=item_id_field
    )

    by_id: dict[str, dict[str, object]] = {}
    by_match_value: dict[str, dict[str, object]] = {}
    for item in items:
        item_id = str(item[item_id_field])
        field_data = item.get(field_data_field)
        if not isinstance(field_data, Mapping):
            raise CMSSnapshotError(
                f"CMS item {item_id!r} has no {field_data_field!r} object"
            )
        match_value = field_data.get(match_field)
        if not isinstance(match_value, str) or not match_value.strip():
            raise CMSSnapshotError(
                f"CMS item {item_id!r} has no nonempty match field {match_field!r}"
            )
        match_value = match_value.strip()
        if match_value in by_match_value:
            other_id = by_match_value[match_value][item_id_field]
            raise CMSSnapshotError(
                f"CMS match value {match_value!r} is duplicated by items "
                f"{other_id!r} and {item_id!r}"
            )
        by_id[item_id] = item
        by_match_value[match_value] = item

    changes: list[dict[str, object]] = []
    matched_item_ids: set[str] = set()
    fields = config["fields"]
    selectors = config["jurisdiction_mappings"]
    assert isinstance(fields, Mapping)
    assert isinstance(selectors, Mapping)
    for row in jurisdictions:
        jurisdiction = str(row["jurisdiction"])
        selector = selectors[jurisdiction]
        assert isinstance(selector, Mapping)
        if "item_id" in selector:
            selector_kind = "item_id"
            selector_value = str(selector["item_id"])
            item = by_id.get(selector_value)
        else:
            selector_kind = "match_value"
            selector_value = str(selector["match_value"])
            item = by_match_value.get(selector_value)
        if item is None:
            raise UnknownJurisdictionError(
                f"No CMS item matches jurisdiction {jurisdiction!r} by "
                f"{selector_kind} {selector_value!r}"
            )
        item_id = str(item[item_id_field])
        if item_id in matched_item_ids:
            raise MappingConfigurationError(
                f"More than one jurisdiction resolves to CMS item {item_id!r}"
            )
        matched_item_ids.add(item_id)
        field_data = item[field_data_field]
        assert isinstance(field_data, Mapping)
        field_changes: dict[str, dict[str, object]] = {}
        for source_field, target_field_raw in sorted(fields.items()):
            target_field = str(target_field_raw)
            after = _source_value(str(source_field), row, metadata)
            canonical_json_bytes(after)
            before = field_data.get(target_field)
            if before != after:
                field_changes[target_field] = {
                    "source_field": source_field,
                    "before": before,
                    "after": after,
                }
        if field_changes:
            changes.append(
                {
                    "item_id": item_id,
                    "jurisdiction": jurisdiction,
                    "matched_by": {
                        "field": item_id_field
                        if selector_kind == "item_id"
                        else match_field,
                        "value": selector_value,
                    },
                    "fields": field_changes,
                }
            )

    unmatched_items = [
        item for item in items if str(item[item_id_field]) not in matched_item_ids
    ]
    if unmatched_items:
        unknown = ", ".join(
            f"{item[item_id_field]}={item[field_data_field][match_field]!r}"
            for item in unmatched_items
        )
        raise UnknownJurisdictionError(
            "CMS snapshot contains items absent from jurisdiction totals: " + unknown
        )

    changes.sort(key=lambda change: str(change["jurisdiction"]))
    target = {key: config[key] for key in ("site_id", "collection_id") if key in config}
    plan: dict[str, Any] = {
        "plan_version": 1,
        "source_manifest_sha256": metadata["source_manifest_sha256"],
        "dedupe_profile": metadata["dedupe_profile"],
        "cutoff_year": metadata["cutoff_year"],
        "provisional": metadata.get("provisional", False),
        "complete_after": metadata.get("complete_after"),
        "cms_snapshot_sha256": canonical_sha256(normalized_snapshot),
        "mapping_sha256": canonical_sha256(config),
        "cms_shape": {
            "item_id_field": item_id_field,
            "field_data_field": field_data_field,
            "jurisdiction_match_field": match_field,
        },
        "target": target,
        "jurisdiction_count": len(jurisdictions),
        "change_count": len(changes),
        "changes": changes,
    }
    plan["plan_sha256"] = canonical_sha256(plan)
    return plan


def verify_change_plan(
    plan: Mapping[str, object], current_cms_snapshot: object
) -> None:
    """Fail unless a plan is intact and the complete CMS state is unchanged."""

    expected_plan_digest = plan.get("plan_sha256")
    if not isinstance(expected_plan_digest, str) or not _SHA256_RE.fullmatch(
        expected_plan_digest
    ):
        raise WebflowPlanError("Plan has no valid plan_sha256")
    digest_input = dict(plan)
    digest_input.pop("plan_sha256", None)
    actual_plan_digest = canonical_sha256(digest_input)
    if actual_plan_digest != expected_plan_digest:
        raise WebflowPlanError("Plan content does not match plan_sha256")

    cms_shape = plan.get("cms_shape")
    if not isinstance(cms_shape, Mapping):
        raise WebflowPlanError("Plan has no valid cms_shape")
    item_id_field = cms_shape.get("item_id_field")
    if not isinstance(item_id_field, str) or not item_id_field:
        raise WebflowPlanError("Plan has no valid CMS item ID field")
    expected_snapshot_digest = plan.get("cms_snapshot_sha256")
    if not isinstance(expected_snapshot_digest, str) or not _SHA256_RE.fullmatch(
        expected_snapshot_digest
    ):
        raise WebflowPlanError("Plan has no valid cms_snapshot_sha256")
    actual_snapshot_digest = cms_snapshot_sha256(
        current_cms_snapshot, item_id_field=item_id_field
    )
    if actual_snapshot_digest != expected_snapshot_digest:
        raise CMSStateChangedError(
            "CMS state changed after the plan was built; rebuild and review the plan"
        )
