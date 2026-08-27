"""Cycle-aware Airtable snapshot, plan, and guarded publication orchestration."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping

from .airtable_api import AirtableClient, RecordUpdate
from .normalize import normalized_key
from .webflow import canonical_sha256, validate_jurisdiction_totals

PERMIT_BANDS = ("vli", "li", "mi", "ami")
BASE_METADATA_FIELDS = {
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
BASE_ROW_FIELDS = {
    "jurisdiction",
    "jurisdiction_key",
    "acutely_low",
    "extremely_low",
    "very_low",
    "low",
    "moderate",
    "above_moderate",
    "total",
    "undated_permits",
    "last_updated",
    "data_status",
}
SYNC_CONFIG_FIELDS = {
    "config_version",
    "base_id",
    "jurisdictions_table_id",
    "rhna_table_id",
    "expected_jurisdiction_record_count",
    "excluded_jurisdictions",
    "jurisdiction_fields",
    "rhna_fields",
}
JURISDICTION_FIELD_NAMES = {"name", "unincorporated"}
RHNA_FIELD_NAMES = {
    "jurisdiction_link",
    "cycle_link",
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
JURISDICTION_FIELD_TYPES = {
    "name": "singleLineText",
    "unincorporated": "checkbox",
}
RHNA_FIELD_TYPES = {
    "jurisdiction_link": "multipleRecordLinks",
    "cycle_link": "multipleRecordLinks",
    "cycle": "multipleLookupValues",
    "current": "formula",
    "correct_link": "formula",
    "vli": "number",
    "li": "number",
    "mi": "number",
    "ami": "number",
    "rhna_start": "multipleLookupValues",
    "rhna_end": "multipleLookupValues",
    "total_progress_override": "number",
}
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_GIT_SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
_UNINCORPORATED_SUFFIX_RE = re.compile(
    r"\s*\(\s*Unincorporated(?:\s+Areas?)?\s*\)\s*$", re.IGNORECASE
)


class AirtableSyncError(ValueError):
    """Base class for safe publication planning errors."""


class AirtableSyncConfigurationError(AirtableSyncError):
    """The pinned Airtable mapping or schema is invalid."""


class AirtableSyncSnapshotError(AirtableSyncError):
    """A complete Airtable snapshot could not be proven."""


class AirtableSyncCoverageError(AirtableSyncError):
    """The source cannot be matched to exactly one intended Airtable row."""


class AirtableSyncBlockedError(AirtableSyncError):
    """A valid plan contains quality blockers and cannot be applied."""


class AirtableSyncStateChangedError(AirtableSyncError):
    """Airtable changed between planning and publication."""


def _strict_keys(value: Mapping[str, object], expected: set[str], label: str) -> None:
    missing = sorted(expected - set(value))
    extra = sorted(set(value) - expected)
    if missing or extra:
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if extra:
            details.append("unexpected " + ", ".join(extra))
        raise AirtableSyncConfigurationError(f"{label}: " + "; ".join(details))


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AirtableSyncConfigurationError(f"{label} must be a nonempty string")
    return value.strip()


def _field_mapping(
    value: object, expected_names: set[str], label: str
) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise AirtableSyncConfigurationError(f"{label} must be an object")
    raw = dict(value)
    _strict_keys(raw, expected_names, label)
    result = {name: _string(raw[name], f"{label}.{name}") for name in expected_names}
    if len(set(result.values())) != len(result):
        raise AirtableSyncConfigurationError(f"{label} repeats a field ID")
    if any(not field_id.startswith("fld") for field_id in result.values()):
        raise AirtableSyncConfigurationError(f"{label} contains an invalid field ID")
    return dict(sorted(result.items()))


def normalize_sync_config(config: Mapping[str, object]) -> dict[str, object]:
    """Validate and canonicalize the public, nonsecret Airtable mapping."""

    if not isinstance(config, Mapping):
        raise AirtableSyncConfigurationError("Airtable sync config must be an object")
    raw = dict(config)
    _strict_keys(raw, SYNC_CONFIG_FIELDS, "Airtable sync config")
    if raw["config_version"] != 1:
        raise AirtableSyncConfigurationError("config_version must be 1")
    count = raw["expected_jurisdiction_record_count"]
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise AirtableSyncConfigurationError(
            "expected_jurisdiction_record_count must be positive"
        )
    excluded_raw = raw["excluded_jurisdictions"]
    if not isinstance(excluded_raw, list):
        raise AirtableSyncConfigurationError("excluded_jurisdictions must be an array")
    excluded = [_string(item, "excluded jurisdiction") for item in excluded_raw]
    if len(set(excluded)) != len(excluded):
        raise AirtableSyncConfigurationError("excluded_jurisdictions has duplicates")
    result: dict[str, object] = {
        "config_version": 1,
        "base_id": _string(raw["base_id"], "base_id"),
        "jurisdictions_table_id": _string(
            raw["jurisdictions_table_id"], "jurisdictions_table_id"
        ),
        "rhna_table_id": _string(raw["rhna_table_id"], "rhna_table_id"),
        "expected_jurisdiction_record_count": count,
        "excluded_jurisdictions": sorted(excluded),
        "jurisdiction_fields": _field_mapping(
            raw["jurisdiction_fields"],
            JURISDICTION_FIELD_NAMES,
            "jurisdiction_fields",
        ),
        "rhna_fields": _field_mapping(
            raw["rhna_fields"], RHNA_FIELD_NAMES, "rhna_fields"
        ),
    }
    if not str(result["base_id"]).startswith("app"):
        raise AirtableSyncConfigurationError("base_id is not an Airtable base ID")
    for table_key in ("jurisdictions_table_id", "rhna_table_id"):
        if not str(result[table_key]).startswith("tbl"):
            raise AirtableSyncConfigurationError(f"{table_key} is invalid")
    return result


def _schema_types(schema: Mapping[str, object], table_id: str) -> dict[str, str]:
    if schema.get("complete") is not True or schema.get("table_id") != table_id:
        raise AirtableSyncSnapshotError(
            "Airtable schema is incomplete or for another table"
        )
    fields = schema.get("fields")
    if not isinstance(fields, list):
        raise AirtableSyncSnapshotError("Airtable schema has no fields array")
    result: dict[str, str] = {}
    for item in fields:
        if not isinstance(item, Mapping):
            raise AirtableSyncSnapshotError(
                "Airtable schema contains a malformed field"
            )
        field_id = item.get("id")
        field_type = item.get("type")
        if not isinstance(field_id, str) or not isinstance(field_type, str):
            raise AirtableSyncSnapshotError("Airtable schema field is incomplete")
        if field_id in result:
            raise AirtableSyncSnapshotError("Airtable schema repeats a field ID")
        result[field_id] = field_type
    return result


def _verify_schema(
    schema: Mapping[str, object],
    *,
    table_id: str,
    field_ids: Mapping[str, str],
    expected_types: Mapping[str, str],
) -> None:
    actual = _schema_types(schema, table_id)
    for logical_name, field_id in field_ids.items():
        if actual.get(field_id) != expected_types[logical_name]:
            raise AirtableSyncSnapshotError(
                f"Airtable field {logical_name!r} no longer has the pinned type"
            )


def _complete_export(
    export: Mapping[str, object], *, base_id: str, table_id: str
) -> list[dict[str, object]]:
    if (
        export.get("complete") is not True
        or export.get("terminal_offset_reached") is not True
        or export.get("base_id") != base_id
        or export.get("table_id") != table_id
    ):
        raise AirtableSyncSnapshotError("Airtable pagination is incomplete")
    records = export.get("records")
    count = export.get("record_count")
    if not isinstance(records, list) or count != len(records):
        raise AirtableSyncSnapshotError("Airtable record count does not reconcile")
    result: list[dict[str, object]] = []
    seen: set[str] = set()
    for item in records:
        if not isinstance(item, Mapping):
            raise AirtableSyncSnapshotError(
                "Airtable export contains a malformed record"
            )
        record_id = item.get("id")
        fields = item.get("fields")
        if not isinstance(record_id, str) or not record_id.startswith("rec"):
            raise AirtableSyncSnapshotError(
                "Airtable export contains an invalid record ID"
            )
        if record_id in seen or not isinstance(fields, Mapping):
            raise AirtableSyncSnapshotError("Airtable export is duplicate or malformed")
        seen.add(record_id)
        result.append({"record_id": record_id, "fields": dict(fields)})
    result.sort(key=lambda row: str(row["record_id"]))
    return result


def _integer(value: object, label: str, *, optional: bool = False) -> int | None:
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AirtableSyncSnapshotError(f"{label} must be an integer")
    integer = int(value)
    if integer != value or integer < 0:
        raise AirtableSyncSnapshotError(f"{label} must be a nonnegative integer")
    return integer


def _formula_boolean(value: object, label: str) -> bool:
    if isinstance(value, bool) or value not in (0, 1):
        raise AirtableSyncSnapshotError(f"{label} must be numeric zero or one")
    return bool(value)


def _links(value: object, label: str) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.startswith("rec") for item in value
    ):
        raise AirtableSyncSnapshotError(f"{label} must contain Airtable record IDs")
    return list(value)


def _lookup_values(value: object, label: str) -> list[object]:
    # Airtable's public REST API returns lookup cells as flat arrays. The
    # connector used for interactive audits preserves the linked-record
    # relationship in a wrapper. Accept both documented surfaces while
    # keeping the wrapper reconciliation checks strict.
    if isinstance(value, list):
        return list(value)
    if not isinstance(value, Mapping):
        raise AirtableSyncSnapshotError(f"{label} is not an Airtable lookup value")
    linked = value.get("linkedRecordIds")
    by_link = value.get("valuesByLinkedRecordId")
    if not isinstance(linked, list) or not isinstance(by_link, Mapping):
        raise AirtableSyncSnapshotError(f"{label} lookup wrapper is malformed")
    if any(not isinstance(item, str) or not item.startswith("rec") for item in linked):
        raise AirtableSyncSnapshotError(f"{label} lookup links are malformed")
    if set(by_link) != set(linked):
        raise AirtableSyncSnapshotError(f"{label} lookup links do not reconcile")
    values: list[object] = []
    for record_id in linked:
        linked_values = by_link[record_id]
        if not isinstance(linked_values, list):
            raise AirtableSyncSnapshotError(f"{label} lookup values are malformed")
        values.extend(linked_values)
    return values


def _cycle(value: object, label: str) -> str:
    values = _lookup_values(value, label)
    if len(values) != 1:
        raise AirtableSyncSnapshotError(f"{label} must resolve to one select value")
    selected = values[0]
    if isinstance(selected, Mapping):
        name = selected.get("name")
    elif isinstance(selected, str):
        name = selected
    else:
        raise AirtableSyncSnapshotError(f"{label} must resolve to one select value")
    # Fifth-cycle rows remain in the historical table. They are never selected
    # as publication targets, but the complete snapshot must still parse them.
    if name not in {"5th", "6th", "7th"}:
        raise AirtableSyncSnapshotError(f"{label} has an unsupported cycle")
    return str(name)


def _lookup_date(value: object | None, label: str) -> str | None:
    if value is None:
        return None
    values = _lookup_values(value, label)
    if len(values) != 1 or not isinstance(values[0], str):
        raise AirtableSyncSnapshotError(f"{label} must resolve to one date")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", values[0]):
        raise AirtableSyncSnapshotError(f"{label} is not an ISO date")
    return values[0]


def project_airtable_snapshot(
    *,
    config: Mapping[str, object],
    jurisdiction_schema: Mapping[str, object],
    rhna_schema: Mapping[str, object],
    jurisdiction_export: Mapping[str, object],
    rhna_export: Mapping[str, object],
) -> dict[str, object]:
    """Project complete raw API exports into stable, reviewable state."""

    normalized = normalize_sync_config(config)
    base_id = str(normalized["base_id"])
    jurisdictions_table_id = str(normalized["jurisdictions_table_id"])
    rhna_table_id = str(normalized["rhna_table_id"])
    jurisdiction_fields = normalized["jurisdiction_fields"]
    rhna_fields = normalized["rhna_fields"]
    assert isinstance(jurisdiction_fields, Mapping)
    assert isinstance(rhna_fields, Mapping)
    _verify_schema(
        jurisdiction_schema,
        table_id=jurisdictions_table_id,
        field_ids=jurisdiction_fields,
        expected_types=JURISDICTION_FIELD_TYPES,
    )
    _verify_schema(
        rhna_schema,
        table_id=rhna_table_id,
        field_ids=rhna_fields,
        expected_types=RHNA_FIELD_TYPES,
    )
    jurisdiction_records = _complete_export(
        jurisdiction_export, base_id=base_id, table_id=jurisdictions_table_id
    )
    rhna_records_raw = _complete_export(
        rhna_export, base_id=base_id, table_id=rhna_table_id
    )
    if len(jurisdiction_records) != normalized["expected_jurisdiction_record_count"]:
        raise AirtableSyncSnapshotError(
            "Airtable jurisdiction count changed; review the mapping before publication"
        )

    jurisdictions: list[dict[str, object]] = []
    for item in jurisdiction_records:
        fields = item["fields"]
        assert isinstance(fields, Mapping)
        name = fields.get(jurisdiction_fields["name"])
        if not isinstance(name, str) or not name.strip():
            raise AirtableSyncSnapshotError("Airtable jurisdiction name is blank")
        unincorporated_raw = fields.get(jurisdiction_fields["unincorporated"], False)
        if not isinstance(unincorporated_raw, bool):
            raise AirtableSyncSnapshotError(
                "Airtable unincorporated flag is not a checkbox value"
            )
        jurisdictions.append(
            {
                "record_id": item["record_id"],
                "name": name.strip(),
                "unincorporated": unincorporated_raw,
            }
        )

    rhna_records: list[dict[str, object]] = []
    for item in rhna_records_raw:
        fields = item["fields"]
        assert isinstance(fields, Mapping)
        record_id = str(item["record_id"])
        permit_values = {
            band: _integer(
                fields.get(rhna_fields[band]),
                f"RHNA record {record_id}.{band}",
                optional=True,
            )
            for band in PERMIT_BANDS
        }
        override = _integer(
            fields.get(rhna_fields["total_progress_override"]),
            f"RHNA record {record_id}.total_progress_override",
            optional=True,
        )
        rhna_records.append(
            {
                "record_id": record_id,
                "jurisdiction_record_ids": _links(
                    fields.get(rhna_fields["jurisdiction_link"]),
                    f"RHNA record {record_id}.jurisdiction_link",
                ),
                "cycle_record_ids": _links(
                    fields.get(rhna_fields["cycle_link"]),
                    f"RHNA record {record_id}.cycle_link",
                ),
                "cycle": _cycle(
                    fields.get(rhna_fields["cycle"]),
                    f"RHNA record {record_id}.cycle",
                ),
                "current": _formula_boolean(
                    fields.get(rhna_fields["current"]),
                    f"RHNA record {record_id}.current",
                ),
                "correct_link": _formula_boolean(
                    fields.get(rhna_fields["correct_link"]),
                    f"RHNA record {record_id}.correct_link",
                ),
                "permit_values": permit_values,
                "rhna_start": _lookup_date(
                    fields.get(rhna_fields["rhna_start"]),
                    f"RHNA record {record_id}.rhna_start",
                ),
                "rhna_end": _lookup_date(
                    fields.get(rhna_fields["rhna_end"]),
                    f"RHNA record {record_id}.rhna_end",
                ),
                "total_progress_override": override,
            }
        )

    retrieved_values = [
        jurisdiction_export.get("retrieved_at"),
        rhna_export.get("retrieved_at"),
    ]
    if any(not isinstance(value, str) or not value for value in retrieved_values):
        raise AirtableSyncSnapshotError("Airtable exports lack retrieval timestamps")
    snapshot = {
        "snapshot_version": 2,
        "complete": True,
        "retrieved_at": max(str(value) for value in retrieved_values),
        "base_id": base_id,
        "jurisdictions_table_id": jurisdictions_table_id,
        "rhna_table_id": rhna_table_id,
        "schema_sha256": canonical_sha256(
            {"jurisdictions": jurisdiction_schema, "rhna": rhna_schema}
        ),
        "record_counts": {
            "jurisdictions": len(jurisdictions),
            "rhna_records": len(rhna_records),
        },
        "jurisdictions": sorted(jurisdictions, key=lambda row: str(row["record_id"])),
        "rhna_records": sorted(rhna_records, key=lambda row: str(row["record_id"])),
    }
    canonical_sha256(snapshot)
    return snapshot


def fetch_airtable_snapshot(
    *, token: str, config: Mapping[str, object]
) -> tuple[dict[str, object], AirtableClient]:
    """Fetch schema and every record from both required Airtable tables."""

    normalized = normalize_sync_config(config)
    jurisdiction_fields = normalized["jurisdiction_fields"]
    rhna_fields = normalized["rhna_fields"]
    assert isinstance(jurisdiction_fields, Mapping)
    assert isinstance(rhna_fields, Mapping)
    jurisdiction_client = AirtableClient(
        token=token,
        base_id=str(normalized["base_id"]),
        table_id=str(normalized["jurisdictions_table_id"]),
        managed_field_ids=list(jurisdiction_fields.values()),
    )
    rhna_client = AirtableClient(
        token=token,
        base_id=str(normalized["base_id"]),
        table_id=str(normalized["rhna_table_id"]),
        managed_field_ids=list(rhna_fields.values()),
    )
    jurisdiction_schema = jurisdiction_client.get_table_schema()
    rhna_schema = rhna_client.get_table_schema()
    jurisdiction_export = jurisdiction_client.list_records(verify_schema=False)
    rhna_export = rhna_client.list_records(verify_schema=False)
    return (
        project_airtable_snapshot(
            config=normalized,
            jurisdiction_schema=jurisdiction_schema,
            rhna_schema=rhna_schema,
            jurisdiction_export=jurisdiction_export,
            rhna_export=rhna_export,
        ),
        rhna_client,
    )


def _validate_source(
    totals: Mapping[str, object],
) -> tuple[dict[str, object], list[dict[str, object]]]:
    metadata_raw = totals.get("metadata")
    rows_raw = totals.get("jurisdictions")
    if not isinstance(metadata_raw, Mapping) or not isinstance(rows_raw, list):
        raise AirtableSyncError("Airtable totals artifact is malformed")
    metadata = dict(metadata_raw)
    expected_metadata = BASE_METADATA_FIELDS | {"cycle_policy_sha256", "cycle_counts"}
    if set(metadata) != expected_metadata:
        raise AirtableSyncError("Airtable totals metadata fields changed")
    cycle_digest = metadata["cycle_policy_sha256"]
    if not isinstance(cycle_digest, str) or not _SHA256_RE.fullmatch(cycle_digest):
        raise AirtableSyncError("Airtable cycle policy digest is invalid")
    counts = metadata["cycle_counts"]
    if not isinstance(counts, Mapping) or set(counts) != {"6th", "7th"}:
        raise AirtableSyncError("Airtable cycle counts are invalid")
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in counts.values()
    ):
        raise AirtableSyncError("Airtable cycle counts must be nonnegative integers")
    base_payload = {
        "metadata": {field: metadata[field] for field in BASE_METADATA_FIELDS},
        "jurisdictions": [],
    }
    extras_by_key: dict[str, dict[str, object]] = {}
    for position, raw in enumerate(rows_raw):
        if not isinstance(raw, Mapping):
            raise AirtableSyncError(f"Airtable totals row {position} is malformed")
        row = dict(raw)
        if set(row) != BASE_ROW_FIELDS | {"cycle", "period_start", "period_end"}:
            raise AirtableSyncError(f"Airtable totals row {position} fields changed")
        key = row.get("jurisdiction_key")
        if (
            not isinstance(key, str)
            or normalized_key(key) != key
            or key in extras_by_key
        ):
            raise AirtableSyncError(f"Airtable totals row {position} key is invalid")
        cycle = row.get("cycle")
        if cycle not in {"6th", "7th"}:
            raise AirtableSyncError(f"Airtable totals row {position} cycle is invalid")
        for field in ("period_start", "period_end"):
            if not isinstance(row.get(field), str) or not re.fullmatch(
                r"\d{4}-\d{2}-\d{2}", str(row[field])
            ):
                raise AirtableSyncError(
                    f"Airtable totals row {position} {field} is invalid"
                )
        extras_by_key[key] = {
            "cycle": cycle,
            "period_start": row["period_start"],
            "period_end": row["period_end"],
        }
        base_payload["jurisdictions"].append(
            {field: row[field] for field in BASE_ROW_FIELDS}
        )
    base_metadata, base_rows = validate_jurisdiction_totals(base_payload)
    actual_counts = Counter(
        str(extras_by_key[str(row["jurisdiction_key"])]["cycle"]) for row in base_rows
    )
    if dict(sorted(actual_counts.items())) != dict(sorted(counts.items())):
        raise AirtableSyncError("Airtable source cycle counts do not reconcile")
    for row in base_rows:
        row.update(extras_by_key[str(row["jurisdiction_key"])])
    return {
        **base_metadata,
        "cycle_policy_sha256": cycle_digest,
        "cycle_counts": dict(counts),
    }, base_rows


def _airtable_identity(name: str, unincorporated: bool) -> tuple[str, bool]:
    stripped = _UNINCORPORATED_SUFFIX_RE.sub("", name).strip()
    return normalized_key(stripped), unincorporated


def _source_identity(row: Mapping[str, object]) -> tuple[str, bool]:
    key = str(row["jurisdiction_key"])
    return key, key.endswith(" COUNTY")


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
        raise AirtableSyncError(
            f"Airtable permit bands do not reconcile for {row['jurisdiction']!r}"
        )
    return result


def airtable_sync_snapshot_sha256(snapshot: Mapping[str, object]) -> str:
    digest_input = dict(snapshot)
    digest_input.pop("retrieved_at", None)
    return canonical_sha256(digest_input)


def build_airtable_sync_plan(
    *,
    totals: Mapping[str, object],
    snapshot: Mapping[str, object],
    config: Mapping[str, object],
    git_sha: str,
) -> dict[str, object]:
    """Build a digest-bound mixed-cycle update plan without sending writes."""

    if not _GIT_SHA_RE.fullmatch(git_sha):
        raise AirtableSyncConfigurationError("git_sha must be a full lowercase SHA")
    metadata, source_rows = _validate_source(totals)
    normalized = normalize_sync_config(config)
    if snapshot.get("complete") is not True or snapshot.get("snapshot_version") != 2:
        raise AirtableSyncSnapshotError("Airtable snapshot is not complete version 2")
    for key in ("base_id", "jurisdictions_table_id", "rhna_table_id"):
        if snapshot.get(key) != normalized[key]:
            raise AirtableSyncSnapshotError(f"Airtable snapshot {key} changed")
    jurisdictions = snapshot.get("jurisdictions")
    rhna_records = snapshot.get("rhna_records")
    counts = snapshot.get("record_counts")
    if not isinstance(jurisdictions, list) or not isinstance(rhna_records, list):
        raise AirtableSyncSnapshotError("Airtable snapshot arrays are missing")
    if (
        not isinstance(counts, Mapping)
        or counts.get("jurisdictions") != len(jurisdictions)
        or counts.get("rhna_records") != len(rhna_records)
    ):
        raise AirtableSyncSnapshotError("Airtable snapshot counts do not reconcile")

    excluded = set(normalized["excluded_jurisdictions"])
    by_identity: dict[tuple[str, bool], dict[str, object]] = {}
    excluded_found: set[str] = set()
    all_by_id: dict[str, dict[str, object]] = {}
    for raw in jurisdictions:
        if not isinstance(raw, Mapping):
            raise AirtableSyncSnapshotError(
                "Airtable jurisdiction snapshot is malformed"
            )
        row = dict(raw)
        record_id = row.get("record_id")
        name = row.get("name")
        unincorporated = row.get("unincorporated")
        if (
            not isinstance(record_id, str)
            or not isinstance(name, str)
            or not isinstance(unincorporated, bool)
        ):
            raise AirtableSyncSnapshotError(
                "Airtable jurisdiction snapshot is malformed"
            )
        all_by_id[record_id] = row
        if name in excluded:
            excluded_found.add(name)
            continue
        identity = _airtable_identity(name, unincorporated)
        if identity in by_identity:
            raise AirtableSyncCoverageError(
                "Airtable has duplicate canonical jurisdiction identities"
            )
        by_identity[identity] = row
    if excluded_found != excluded:
        raise AirtableSyncCoverageError("Configured Airtable exclusions are absent")

    source_by_key: dict[str, dict[str, object]] = {}
    targets: dict[str, dict[str, object]] = {}
    used_identities: set[tuple[str, bool]] = set()
    for row in source_rows:
        key = str(row["jurisdiction_key"])
        if key in source_by_key:
            raise AirtableSyncCoverageError("Source repeats a jurisdiction key")
        source_by_key[key] = row
        identity = _source_identity(row)
        target = by_identity.get(identity)
        if target is None:
            raise AirtableSyncCoverageError(
                f"No exact Airtable jurisdiction matches {key!r}"
            )
        if identity in used_identities:
            raise AirtableSyncCoverageError("Source jurisdictions map ambiguously")
        used_identities.add(identity)
        targets[key] = target
    unexpected = sorted(set(by_identity) - used_identities)
    if unexpected:
        raise AirtableSyncCoverageError(
            "Airtable has jurisdictions outside the reviewed source mapping"
        )

    rhna_by_target: dict[tuple[str, str], dict[str, object]] = {}
    for raw in rhna_records:
        if not isinstance(raw, Mapping):
            raise AirtableSyncSnapshotError("Airtable RHNA snapshot is malformed")
        row = dict(raw)
        links = row.get("jurisdiction_record_ids")
        cycle = row.get("cycle")
        if not isinstance(links, list) or len(links) != 1 or not isinstance(cycle, str):
            continue
        jurisdiction_id = str(links[0])
        if jurisdiction_id not in all_by_id:
            raise AirtableSyncCoverageError(
                "Airtable RHNA row links to an unknown jurisdiction"
            )
        target_key = (jurisdiction_id, cycle)
        if target_key in rhna_by_target:
            raise AirtableSyncCoverageError(
                "Airtable has duplicate RHNA rows for a jurisdiction and cycle"
            )
        rhna_by_target[target_key] = row

    rhna_fields = normalized["rhna_fields"]
    assert isinstance(rhna_fields, Mapping)
    changes: list[dict[str, object]] = []
    unchanged_count = 0
    blockers: list[dict[str, object]] = []
    not_current: list[str] = []
    incorrect_links: list[str] = []
    overrides: list[str] = []
    before_aggregate = {band: 0 for band in PERMIT_BANDS}
    after_aggregate = {band: 0 for band in PERMIT_BANDS}
    for key in sorted(source_by_key):
        source = source_by_key[key]
        target = targets[key]
        cycle = str(source["cycle"])
        rhna = rhna_by_target.get((str(target["record_id"]), cycle))
        if rhna is None:
            raise AirtableSyncCoverageError(
                f"Airtable has no {cycle} RHNA row for {key!r}"
            )
        if rhna.get("correct_link") is not True:
            incorrect_links.append(key)
        if rhna.get("current") is not True:
            not_current.append(key)
        if rhna.get("total_progress_override") not in (None, 0):
            overrides.append(key)
        permit_values = rhna.get("permit_values")
        if not isinstance(permit_values, Mapping) or set(permit_values) != set(
            PERMIT_BANDS
        ):
            raise AirtableSyncSnapshotError("Airtable permit values are malformed")
        before_raw = {band: permit_values[band] for band in PERMIT_BANDS}
        before = {band: int(before_raw[band] or 0) for band in PERMIT_BANDS}
        after = _target_bands(source)
        for band in PERMIT_BANDS:
            before_aggregate[band] += before[band]
            after_aggregate[band] += after[band]
        changed = [band for band in PERMIT_BANDS if before[band] != after[band]]
        if not changed:
            unchanged_count += 1
            continue
        changes.append(
            {
                "airtable_record_id": rhna["record_id"],
                "jurisdiction_record_id": target["record_id"],
                "jurisdiction_key": key,
                "cycle": cycle,
                "expected_fields": {
                    rhna_fields[band]: before_raw[band] for band in changed
                },
                "desired_fields": {rhna_fields[band]: after[band] for band in changed},
                "before_total": sum(before.values()),
                "after_total": sum(after.values()),
                "data_status": source["data_status"],
                "undated_permits": source["undated_permits"],
            }
        )

    if metadata["provisional"] is True:
        blockers.append(
            {
                "code": "source_is_provisional",
                "message": "The selected reporting year is not yet complete under HCD policy.",
            }
        )
    if incorrect_links:
        blockers.append(
            {
                "code": "airtable_incorrect_links",
                "message": "Target RHNA rows fail Airtable's Correct Link check.",
                "jurisdiction_keys": incorrect_links,
            }
        )
    if not_current:
        blockers.append(
            {
                "code": "airtable_target_not_current",
                "message": "Target RHNA rows are not marked current by Airtable.",
                "jurisdiction_keys": not_current,
            }
        )
    if overrides:
        blockers.append(
            {
                "code": "airtable_progress_overrides_present",
                "message": "Progress overrides would supersede the published permit bands.",
                "jurisdiction_keys": overrides,
            }
        )
    changes.sort(key=lambda item: str(item["jurisdiction_key"]))
    plan: dict[str, object] = {
        "plan_version": 2,
        "intent": "update_existing_permit_fields_only",
        "apply_eligible": not blockers,
        "git_sha": git_sha,
        "source_sha256": canonical_sha256(totals),
        "source_manifest_sha256": metadata["source_manifest_sha256"],
        "cycle_policy_sha256": metadata["cycle_policy_sha256"],
        "dedupe_profile": metadata["dedupe_profile"],
        "cutoff_year": metadata["cutoff_year"],
        "source_last_updated": metadata["last_updated"],
        "airtable_retrieved_at": snapshot["retrieved_at"],
        "airtable_snapshot_sha256": airtable_sync_snapshot_sha256(snapshot),
        "airtable_schema_sha256": snapshot["schema_sha256"],
        "config_sha256": canonical_sha256(normalized),
        "target": {
            "base_id": normalized["base_id"],
            "rhna_table_id": normalized["rhna_table_id"],
            "field_ids": {band: rhna_fields[band] for band in PERMIT_BANDS},
        },
        "matched_count": len(source_rows),
        "cycle_counts": metadata["cycle_counts"],
        "change_count": len(changes),
        "unchanged_count": unchanged_count,
        "airtable_aggregate_before": {
            **before_aggregate,
            "total": sum(before_aggregate.values()),
        },
        "source_aggregate_after": {
            **after_aggregate,
            "total": sum(after_aggregate.values()),
        },
        "blockers": blockers,
        "changes": changes,
    }
    plan["plan_sha256"] = canonical_sha256(plan)
    return plan


def verify_airtable_sync_plan(
    plan: Mapping[str, object], current_snapshot: Mapping[str, object]
) -> None:
    digest = plan.get("plan_sha256")
    if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
        raise AirtableSyncError("Airtable sync plan has no valid digest")
    digest_input = dict(plan)
    digest_input.pop("plan_sha256", None)
    if canonical_sha256(digest_input) != digest:
        raise AirtableSyncError("Airtable sync plan digest does not match its content")
    if plan.get("airtable_snapshot_sha256") != airtable_sync_snapshot_sha256(
        current_snapshot
    ):
        raise AirtableSyncStateChangedError(
            "Airtable changed after planning; rebuild the plan before publication"
        )


def plan_record_updates(plan: Mapping[str, object]) -> list[RecordUpdate]:
    """Translate an eligible plan into conditional existing-record updates."""

    if plan.get("apply_eligible") is not True:
        raise AirtableSyncBlockedError("Airtable sync plan contains blockers")
    changes = plan.get("changes")
    if not isinstance(changes, list):
        raise AirtableSyncError("Airtable sync plan changes are malformed")
    updates: list[RecordUpdate] = []
    for change in changes:
        if not isinstance(change, Mapping):
            raise AirtableSyncError("Airtable sync plan change is malformed")
        updates.append(
            RecordUpdate(
                record_id=str(change["airtable_record_id"]),
                expected_fields=dict(change["expected_fields"]),
                desired_fields=dict(change["desired_fields"]),
            )
        )
    return updates


def summarize_airtable_sync_plan(plan: Mapping[str, object]) -> dict[str, object]:
    """Return a log-safe summary that omits record IDs and field values."""

    blockers = plan.get("blockers")
    blocker_codes = []
    if isinstance(blockers, list):
        blocker_codes = sorted(
            str(item.get("code")) for item in blockers if isinstance(item, Mapping)
        )
    return {
        "apply_eligible": plan.get("apply_eligible") is True,
        "matched_count": plan.get("matched_count"),
        "change_count": plan.get("change_count"),
        "unchanged_count": plan.get("unchanged_count"),
        "cycle_counts": plan.get("cycle_counts"),
        "source_total": (
            plan.get("source_aggregate_after", {}).get("total")
            if isinstance(plan.get("source_aggregate_after"), Mapping)
            else None
        ),
        "blocker_codes": blocker_codes,
        "plan_sha256": plan.get("plan_sha256"),
        "source_sha256": plan.get("source_sha256"),
        "airtable_snapshot_sha256": plan.get("airtable_snapshot_sha256"),
    }
