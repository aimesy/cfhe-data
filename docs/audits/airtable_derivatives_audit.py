"""Read-only audit of Airtable passthroughs and repository derivatives.

visibility: non-public:private
classification: archive-internal

The audit never calls an Airtable mutation endpoint. It keeps record data in
memory and emits aggregate findings only.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import time
from collections import Counter, defaultdict, deque
from collections.abc import Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import requests
from jsonschema import Draft202012Validator

BASE_ID = "appjwA9nMuNRewKNo"
API_ROOT = "https://api.airtable.com/v0"
COMPUTED_TYPES = {
    "formula",
    "multipleLookupValues",
    "lookup",
    "rollup",
    "count",
}
ERROR_PATTERN = re.compile(
    r"(?:#ERROR!|#REF!|#VALUE!|#NAME\?|\bNaN\b|\bInfinity\b)", re.IGNORECASE
)
AIRTABLE_ID_PATTERN = re.compile(r"\b(?:tbl|fld|viw)[A-Za-z0-9]{14}\b")
EMAIL_TOKEN_PATTERN = re.compile(
    r"(?<![A-Za-z0-9.!#$%&'*+/=?^_`{|}~-])"
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+"
)

JURISDICTIONS_TABLE = "tblMHCufKHzpCWPFJ"
RHNA_TABLE = "tblOiiBUHLapyf25e"
REPORTS_TABLE = "tbluHP9IDYLJZs9en"
EVENTS_TABLE = "tblKFNniMU7ZgwGWD"
WATCHDOGS_TABLE = "tblCDvJsimCIcM12w"
COUNTIES_TABLE = "tblBTiBcyH8mFsh2d"
CENSUS_TABLE = "tblR7D5S79OxWmypk"

INSUFFICIENT_PREDICTION = "there is not enough prior-cycle data to make a prediction."


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _repo_root(start: Path | None = None) -> Path:
    cursor = (start or Path.cwd()).resolve()
    for candidate in (cursor, *cursor.parents):
        if (candidate / "pyproject.toml").exists() and (
            candidate / "src" / "cfhe_data"
        ).exists():
            return candidate
    raise RuntimeError("Could not locate the cfhe-data repository root")


def _token_path(repo_root: Path) -> Path:
    amybot_root = repo_root.parents[1]
    return amybot_root / ".sensitive" / "credentials" / "cfhe-airtable-pat.txt"


def _load_token(repo_root: Path) -> str:
    path = _token_path(repo_root)
    token = path.read_text(encoding="utf-8").strip()
    if not token:
        raise RuntimeError(f"Airtable token file is empty: {path}")
    return token


class AirtableReader:
    """Small read-only Airtable client with bounded retry behavior."""

    def __init__(self, token: str) -> None:
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {token}"})

    def get(self, url: str, *, params: Mapping[str, Any] | None = None) -> Any:
        last_error: Exception | None = None
        for attempt in range(6):
            try:
                response = self.session.get(url, params=params, timeout=90)
                if response.status_code == 429:
                    delay = float(response.headers.get("Retry-After", "1"))
                    time.sleep(min(delay, 15))
                    continue
                if 500 <= response.status_code < 600:
                    time.sleep(min(2**attempt, 15))
                    continue
                response.raise_for_status()
                return response.json()
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                if attempt == 5:
                    break
                time.sleep(min(2**attempt, 15))
        raise RuntimeError(f"Airtable read failed for {url}") from last_error

    def schema(self) -> dict[str, Any]:
        return self.get(f"{API_ROOT}/meta/bases/{BASE_ID}/tables")

    def records(self, table_id: str) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        params: dict[str, Any] = {
            "pageSize": 100,
            "returnFieldsByFieldId": "true",
        }
        while True:
            payload = self.get(f"{API_ROOT}/{BASE_ID}/{table_id}", params=params)
            records.extend(payload.get("records", []))
            offset = payload.get("offset")
            if not offset:
                return records
            params["offset"] = offset


def _cli_json(
    token: str,
    tool: str,
    args: Mapping[str, Any],
    *,
    timeout: int = 120,
) -> dict[str, Any]:
    env = os.environ.copy()
    env["AIRTABLE_TOKEN"] = token
    executable = shutil.which("airtable-mcp") or shutil.which("airtable-mcp.cmd")
    if not executable:
        raise RuntimeError("airtable-mcp is not available on PATH")
    proc = subprocess.run(
        [executable, tool, "--input", "-", "-q"],
        input=json.dumps(args),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=timeout,
        check=False,
        env=env,
    )
    if proc.returncode != 0:
        message = (proc.stderr or proc.stdout or "unknown error").strip()
        raise RuntimeError(f"airtable-mcp {tool} failed: {message[:500]}")
    return json.loads(proc.stdout)


def _walk_ids(value: Any) -> set[str]:
    return set(AIRTABLE_ID_PATTERN.findall(_canonical(value)))


def _field_maps(
    schema: Mapping[str, Any],
) -> tuple[
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[str, str],
    dict[str, str],
]:
    tables = {table["id"]: table for table in schema["tables"]}
    fields: dict[str, dict[str, Any]] = {}
    field_table: dict[str, str] = {}
    views: dict[str, str] = {}
    for table in schema["tables"]:
        for field in table["fields"]:
            fields[field["id"]] = field
            field_table[field["id"]] = table["id"]
        for view in table.get("views", []):
            views[view["id"]] = table["id"]
    return tables, fields, field_table, views


def _record_maps(
    records_by_table: Mapping[str, list[dict[str, Any]]],
) -> dict[str, dict[str, dict[str, Any]]]:
    return {
        table_id: {record["id"]: record for record in records}
        for table_id, records in records_by_table.items()
    }


def _flatten(value: Any) -> list[Any]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        flattened: list[Any] = []
        for item in value:
            flattened.extend(_flatten(item))
        return flattened
    return [value]


def _multiset(value: Any) -> Counter[str]:
    return Counter(_canonical(item) for item in _flatten(value))


def _same_value(actual: Any, expected: Any, *, tolerance: float = 1e-8) -> bool:
    if isinstance(actual, (int, float)) and isinstance(expected, (int, float)):
        return math.isclose(
            float(actual), float(expected), rel_tol=tolerance, abs_tol=tolerance
        )
    return _multiset(actual) == _multiset(expected)


def _first(value: Any) -> Any:
    items = _flatten(value)
    return items[0] if items else None


def _date_value(value: Any) -> dt.date | None:
    item = _first(value)
    if item in (None, ""):
        return None
    try:
        return dt.date.fromisoformat(str(item)[:10])
    except ValueError:
        return None


def _number_value(value: Any) -> float | None:
    item = _first(value)
    if isinstance(item, (int, float)) and not isinstance(item, bool):
        return float(item)
    return None


def _airtable_round(value: float) -> int:
    """Match Airtable ROUND(number, 0) for the nonnegative metrics audited here."""

    return math.floor(value + 0.5)


def _email_tokens(value: Any) -> list[str]:
    """Extract email-shaped tokens without ever emitting their contents."""

    tokens: list[str] = []
    for item in _flatten(value):
        tokens.extend(EMAIL_TOKEN_PATTERN.findall(str(item)))
    return tokens


def _lookup_same(field: Mapping[str, Any], actual: Any, expected: Any) -> bool:
    """Compare lookup values using Airtable's declared result semantics."""

    result_type = field.get("options", {}).get("result", {}).get("type")

    def normalized(value: Any) -> list[Any]:
        items = _flatten(value)
        if result_type == "date":
            return [str(item)[:10] for item in items]
        return [
            item.get("name", item) if isinstance(item, Mapping) else item
            for item in items
        ]

    actual_items = normalized(actual)
    expected_items = normalized(expected)
    if result_type in {"email", "multipleSelects"}:
        return {_canonical(item) for item in actual_items} == {
            _canonical(item) for item in expected_items
        }
    return Counter(_canonical(item) for item in actual_items) == Counter(
        _canonical(item) for item in expected_items
    )


def _source_values(
    source_records: Mapping[str, dict[str, Any]],
    linked_ids: Iterable[str],
    source_field_id: str,
) -> list[Any]:
    values: list[Any] = []
    for record_id in linked_ids:
        record = source_records.get(record_id)
        if not record:
            continue
        values.extend(_flatten(record.get("fields", {}).get(source_field_id)))
    return values


def _rollup_audit(
    fields: Mapping[str, dict[str, Any]],
    field_table: Mapping[str, str],
    records_by_table: Mapping[str, list[dict[str, Any]]],
    record_maps: Mapping[str, Mapping[str, dict[str, Any]]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Recompute configured rollups from their linked source values."""

    aggregations = {
        "fldrlOiNgBX1aJ5Bi": "average",
        "fldHgOx9FtvHFCl1c": "average",
        "fldp1rdCBrmms78nK": "average",
        "fld46CSUJRpwwNHLp": "average",
        "fldGJMfO6RmpkwwVS": "average",
        "fld5S28sAEoxHmaAP": "average",
        "fldjA2sgc5yImZKSI": "sum_unique",
        "fld4rYAjZgrpvrmc8": "unique_text",
        "fldj89LundQCtgrlO": "max_date",
        "fld3PqVCWyIdu1Um5": "max_date",
        "fldYR0pkTi8gUypPB": "unique_text",
    }
    mismatches: Counter[str] = Counter()
    empty_source_errors: Counter[str] = Counter()
    county_field_counts: dict[str, Counter[str]] = defaultdict(Counter)
    values_checked = 0

    for field_id, aggregation in aggregations.items():
        field = fields[field_id]
        options = field.get("options", {})
        table_id = field_table[field_id]
        link_id = options.get("recordLinkFieldId")
        source_id = options.get("fieldIdInLinkedTable")
        linked_table_id = fields[link_id].get("options", {}).get("linkedTableId")
        targets = record_maps[linked_table_id]

        for record in records_by_table[table_id]:
            values_checked += 1
            linked_ids = record.get("fields", {}).get(link_id, []) or []
            source_values = _source_values(targets, linked_ids, source_id)
            actual = record.get("fields", {}).get(field_id)

            if aggregation in {"average", "sum_unique"}:
                numbers = [
                    float(value)
                    for value in source_values
                    if isinstance(value, (int, float)) and not isinstance(value, bool)
                ]
                if numbers:
                    expected = (
                        sum(numbers) / len(numbers)
                        if aggregation == "average"
                        else sum(set(numbers))
                    )
                    same = isinstance(actual, (int, float)) and math.isclose(
                        float(actual), expected, rel_tol=1e-8, abs_tol=1e-8
                    )
                else:
                    if actual is not None and ERROR_PATTERN.search(str(actual)):
                        empty_source_errors[field_id] += 1
                        same = False
                    elif aggregation == "average":
                        same = actual in (None, "", [])
                    else:
                        same = actual in (None, "", 0, [])
                if field_id in {
                    "fld46CSUJRpwwNHLp",
                    "fldGJMfO6RmpkwwVS",
                    "fld5S28sAEoxHmaAP",
                }:
                    if numbers:
                        county_field_counts[field_id]["populated_group_count"] += 1
                    else:
                        county_field_counts[field_id]["empty_group_count"] += 1
                        if actual in (None, "", []):
                            county_field_counts[field_id][
                                "empty_group_blank_count"
                            ] += 1
                        if actual is not None and ERROR_PATTERN.search(str(actual)):
                            county_field_counts[field_id]["error_count"] += 1
            elif aggregation == "max_date":
                dates = [
                    parsed
                    for value in source_values
                    if (parsed := _date_value(value)) is not None
                ]
                if dates:
                    same = _date_value(actual) == max(dates)
                else:
                    same = actual in (None, "", [])
                    if actual is not None and ERROR_PATTERN.search(str(actual)):
                        empty_source_errors[field_id] += 1
            else:
                expected_labels = {
                    str(
                        value.get("name", value)
                        if isinstance(value, Mapping)
                        else value
                    ).strip()
                    for value in source_values
                    if value not in (None, "")
                }
                if isinstance(actual, str):
                    actual_labels = {
                        value.strip() for value in actual.split(",") if value.strip()
                    }
                else:
                    actual_labels = {
                        str(
                            value.get("name", value)
                            if isinstance(value, Mapping)
                            else value
                        ).strip()
                        for value in _flatten(actual)
                        if value not in (None, "")
                    }
                same = actual_labels == expected_labels

            if not same:
                mismatches[field_id] += 1

    failures = sum(mismatches.values())
    check = _check(
        "passthrough",
        "rollups equal independent source aggregations",
        "fail" if failures else "pass",
        "high" if failures else "info",
        values_checked,
        failures,
        f"{failures} rollup values differ from independent source aggregations",
        affected_fields=[
            {
                "field_id": field_id,
                "field": fields[field_id]["name"],
                "records": count,
            }
            for field_id, count in mismatches.most_common()
        ],
    )
    empty_failures = sum(empty_source_errors.values())
    empty_check = _check(
        "passthrough",
        "empty rollup groups return blanks without formula errors",
        "fail" if empty_failures else "pass",
        "high" if empty_failures else "info",
        values_checked,
        empty_failures,
        f"{empty_failures} rollup values are errors because the linked source set is empty",
        affected_fields=[
            {
                "field_id": field_id,
                "field": fields[field_id]["name"],
                "records": count,
            }
            for field_id, count in empty_source_errors.most_common()
        ],
    )
    return [check, empty_check], {
        "rollup_field_count": len(aggregations),
        "rollup_values_checked": values_checked,
        "rollup_mismatches_by_field": dict(mismatches),
        "empty_rollup_errors_by_field": dict(empty_source_errors),
        "county_rollup_counts": {
            field_id: dict(counts)
            for field_id, counts in sorted(county_field_counts.items())
        },
    }


def _check(
    domain: str,
    name: str,
    status: str,
    severity: str,
    objects_checked: int,
    failures: int,
    evidence: str,
    **extra: Any,
) -> dict[str, Any]:
    result = {
        "domain": domain,
        "check": name,
        "status": status,
        "severity": severity,
        "objects_checked": objects_checked,
        "failures": failures,
        "evidence": evidence,
    }
    result.update(extra)
    return result


def _dependency_audit(
    schema: Mapping[str, Any],
    records_by_table: Mapping[str, list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    tables, fields, field_table, views = _field_maps(schema)
    record_maps = _record_maps(records_by_table)
    checks: list[dict[str, Any]] = []

    computed = [field for field in fields.values() if field["type"] in COMPUTED_TYPES]
    invalid = [
        field for field in computed if field.get("options", {}).get("isValid") is False
    ]
    checks.append(
        _check(
            "schema",
            "computed field validity",
            "fail" if invalid else "pass",
            "medium" if invalid else "info",
            len(computed),
            len(invalid),
            f"{len(invalid)} of {len(computed)} computed fields are schema-invalid",
            affected_fields=[
                {
                    "table": tables[field_table[field["id"]]]["name"],
                    "field": field["name"],
                    "field_id": field["id"],
                }
                for field in invalid
            ],
        )
    )

    missing_refs: list[dict[str, str]] = []
    dependency_edges: list[tuple[str, str]] = []
    for field in computed:
        options = field.get("options", {})
        if field["type"] == "formula":
            refs = options.get("referencedFieldIds", [])
            for ref in refs:
                if (
                    ref not in fields
                    or field_table.get(ref) != field_table[field["id"]]
                ):
                    missing_refs.append({"field_id": field["id"], "missing": ref})
                else:
                    dependency_edges.append((ref, field["id"]))
        elif field["type"] in {"lookup", "multipleLookupValues", "rollup"}:
            link_id = options.get("recordLinkFieldId")
            source_id = options.get("fieldIdInLinkedTable")
            if link_id not in fields:
                missing_refs.append({"field_id": field["id"], "missing": str(link_id)})
                continue
            dependency_edges.append((link_id, field["id"]))
            linked_table = fields[link_id].get("options", {}).get("linkedTableId")
            if source_id not in fields or field_table.get(source_id) != linked_table:
                missing_refs.append(
                    {"field_id": field["id"], "missing": str(source_id)}
                )
            else:
                dependency_edges.append((source_id, field["id"]))
        elif field["type"] == "count":
            link_id = options.get("recordLinkFieldId")
            if link_id not in fields:
                missing_refs.append({"field_id": field["id"], "missing": str(link_id)})
            else:
                dependency_edges.append((link_id, field["id"]))
    checks.append(
        _check(
            "schema",
            "computed dependency references resolve",
            "fail" if missing_refs else "pass",
            "high" if missing_refs else "info",
            len(computed),
            len(missing_refs),
            f"{len(missing_refs)} missing or cross-table-invalid field references",
            affected_fields=missing_refs[:25],
        )
    )

    adjacency: dict[str, list[str]] = defaultdict(list)
    indegree = Counter()
    nodes: set[str] = set()
    for source, target in dependency_edges:
        adjacency[source].append(target)
        indegree[target] += 1
        nodes.update((source, target))
    queue = deque(node for node in nodes if indegree[node] == 0)
    visited = 0
    while queue:
        node = queue.popleft()
        visited += 1
        for target in adjacency[node]:
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target)
    cycle_nodes = len(nodes) - visited
    checks.append(
        _check(
            "schema",
            "computed dependency graph is acyclic",
            "fail" if cycle_nodes else "pass",
            "high" if cycle_nodes else "info",
            len(nodes),
            cycle_nodes,
            f"{cycle_nodes} fields remain in cyclic dependency components",
        )
    )

    link_fields = [
        field for field in fields.values() if field["type"] == "multipleRecordLinks"
    ]
    orphan_links = 0
    duplicate_links = 0
    single_cardinality = 0
    inverse_mismatches = 0
    for field in link_fields:
        table_id = field_table[field["id"]]
        options = field.get("options", {})
        linked_table_id = options.get("linkedTableId")
        inverse_id = options.get("inverseLinkFieldId")
        targets = record_maps.get(linked_table_id, {})
        for record in records_by_table[table_id]:
            linked_ids = record.get("fields", {}).get(field["id"], []) or []
            if len(linked_ids) != len(set(linked_ids)):
                duplicate_links += 1
            if options.get("prefersSingleRecordLink") and len(linked_ids) > 1:
                single_cardinality += 1
            for linked_id in linked_ids:
                target = targets.get(linked_id)
                if target is None:
                    orphan_links += 1
                    continue
                if inverse_id:
                    inverse_values = target.get("fields", {}).get(inverse_id, []) or []
                    if record["id"] not in inverse_values:
                        inverse_mismatches += 1
    link_failures = orphan_links + duplicate_links + inverse_mismatches
    checks.append(
        _check(
            "links",
            "linked record integrity",
            "fail" if link_failures else "pass",
            "high" if link_failures else "info",
            len(link_fields),
            link_failures,
            (
                f"{orphan_links} orphan links, {duplicate_links} duplicate link cells, "
                f"and {inverse_mismatches} inverse-link mismatches"
            ),
        )
    )
    checks.append(
        _check(
            "links",
            "single-link cardinality",
            "warn" if single_cardinality else "pass",
            "medium" if single_cardinality else "info",
            len(link_fields),
            single_cardinality,
            f"{single_cardinality} records exceed a preferred single-record link",
        )
    )

    count_fields = [field for field in fields.values() if field["type"] == "count"]
    count_mismatches = 0
    for field in count_fields:
        table_id = field_table[field["id"]]
        link_id = field.get("options", {}).get("recordLinkFieldId")
        for record in records_by_table[table_id]:
            actual = record.get("fields", {}).get(field["id"], 0) or 0
            expected = len(record.get("fields", {}).get(link_id, []) or [])
            if not _same_value(actual, expected):
                count_mismatches += 1
    checks.append(
        _check(
            "passthrough",
            "count fields equal linked record counts",
            "fail" if count_mismatches else "pass",
            "high" if count_mismatches else "info",
            len(count_fields),
            count_mismatches,
            f"{count_mismatches} count values differ from their linked record cardinality",
        )
    )

    lookup_fields = [
        field
        for field in fields.values()
        if field["type"] in {"lookup", "multipleLookupValues"}
    ]
    lookup_mismatches_by_field: Counter[str] = Counter()
    for field in lookup_fields:
        table_id = field_table[field["id"]]
        options = field.get("options", {})
        link_id = options.get("recordLinkFieldId")
        source_id = options.get("fieldIdInLinkedTable")
        link_field = fields.get(link_id, {})
        linked_table_id = link_field.get("options", {}).get("linkedTableId")
        targets = record_maps.get(linked_table_id, {})
        for record in records_by_table[table_id]:
            linked_ids = record.get("fields", {}).get(link_id, []) or []
            expected = _source_values(targets, linked_ids, source_id)
            actual = record.get("fields", {}).get(field["id"], [])
            if not _lookup_same(field, actual, expected):
                lookup_mismatches_by_field[field["id"]] += 1

    conditional_lookup_ids = {
        "fld8w0FlauVTVabdf",
        "fldQ7JM1GyMUIrbZ1",
        "fldNwN6gtSCFQm4Z6",
        "fldYnPVSHKkXwTQmR",
        "flddeMRE0zJU0z5z2",
        "fldNwU8IziwAtCr37",
        "fldGWoXXPkivxKNXV",
        "fldxluQSDBcvs2y9b",
        "fldYI1ZxexFVELZ7Q",
        "fldPr3YcT1lxG9HET",
        "fldaPeimSXIdRJKuJ",
    }
    raw_lookup_failures = sum(
        count
        for field_id, count in lookup_mismatches_by_field.items()
        if field_id not in conditional_lookup_ids
    )
    filtered_lookup_differences = sum(
        count
        for field_id, count in lookup_mismatches_by_field.items()
        if field_id in conditional_lookup_ids
    )
    checks.append(
        _check(
            "passthrough",
            "ordinary lookup values equal linked source values",
            "fail" if raw_lookup_failures else "pass",
            "high" if raw_lookup_failures else "info",
            sum(field["id"] not in conditional_lookup_ids for field in lookup_fields),
            raw_lookup_failures,
            f"{raw_lookup_failures} record values differ across ordinary lookup fields",
            affected_fields=[
                {
                    "field_id": field_id,
                    "field": fields[field_id]["name"],
                    "records": count,
                }
                for field_id, count in lookup_mismatches_by_field.most_common()
                if field_id not in conditional_lookup_ids
            ][:25],
        )
    )

    limited_slack_failures = 0
    limited_slack_field = fields["fldaPeimSXIdRJKuJ"]
    limited_slack_options = limited_slack_field.get("options", {})
    limited_slack_link = limited_slack_options["recordLinkFieldId"]
    limited_slack_source = limited_slack_options["fieldIdInLinkedTable"]
    limited_slack_table = field_table["fldaPeimSXIdRJKuJ"]
    limited_slack_linked_table = fields[limited_slack_link]["options"]["linkedTableId"]
    limited_slack_targets = record_maps[limited_slack_linked_table]
    for record in records_by_table[limited_slack_table]:
        linked_ids = record.get("fields", {}).get(limited_slack_link, []) or []
        expected = _source_values(
            limited_slack_targets,
            linked_ids[-1:] if linked_ids else [],
            limited_slack_source,
        )
        actual = record.get("fields", {}).get("fldaPeimSXIdRJKuJ", [])
        if not _lookup_same(limited_slack_field, actual, expected):
            limited_slack_failures += 1
    checks.append(
        _check(
            "passthrough",
            "limited Slack lookup equals the last linked jurisdiction",
            "fail" if limited_slack_failures else "pass",
            "high" if limited_slack_failures else "info",
            len(records_by_table[limited_slack_table]),
            limited_slack_failures,
            (
                f"{limited_slack_failures} report Slack values differ from the configured "
                "one-item lookup behavior"
            ),
        )
    )

    rollup_checks, rollup_summary = _rollup_audit(
        fields, field_table, records_by_table, record_maps
    )
    checks.extend(rollup_checks)

    formula_fields = [field for field in fields.values() if field["type"] == "formula"]
    bad_value_counts: Counter[str] = Counter()
    for field in formula_fields:
        table_id = field_table[field["id"]]
        for record in records_by_table[table_id]:
            value = record.get("fields", {}).get(field["id"])
            if value is not None and ERROR_PATTERN.search(str(value)):
                bad_value_counts[field["id"]] += 1
    formula_anomaly_count = sum(bad_value_counts.values())
    checks.append(
        _check(
            "formulas",
            "formula outputs contain no error sentinels",
            "fail" if formula_anomaly_count else "pass",
            "high" if formula_anomaly_count else "info",
            len(formula_fields),
            formula_anomaly_count,
            f"{formula_anomaly_count} formula values contain an error sentinel",
            affected_fields=[
                {
                    "table": tables[field_table[field_id]]["name"],
                    "field": fields[field_id]["name"],
                    "field_id": field_id,
                    "records": count,
                }
                for field_id, count in bad_value_counts.most_common()
            ],
        )
    )

    event_type_blanks: Counter[str] = Counter()
    for record in records_by_table.get("tblKFNniMU7ZgwGWD", []):
        source_value = record.get("fields", {}).get("flduqrqzxxgCgtLdt")
        output_value = record.get("fields", {}).get("fldJMCWhNktqSyjva")
        if source_value not in (None, "", []) and output_value in (None, "", []):
            event_type_blanks[str(source_value)] += 1
    event_type_failures = sum(event_type_blanks.values())
    checks.append(
        _check(
            "formulas",
            "event type formula covers populated source options",
            "fail" if event_type_failures else "pass",
            "medium" if event_type_failures else "info",
            len(records_by_table.get("tblKFNniMU7ZgwGWD", [])),
            event_type_failures,
            f"{event_type_failures} populated event types produce a blank derivative",
            uncovered_source_values=dict(event_type_blanks),
        )
    )

    summary = {
        "dependency_edge_count": len(dependency_edges),
        "filtered_lookup_raw_difference_count": filtered_lookup_differences,
        "lookup_mismatches_by_field": dict(lookup_mismatches_by_field),
        "table_count": len(tables),
        "field_count": len(fields),
        "computed_field_count": len(computed),
        "link_field_count": len(link_fields),
        "view_count": len(views),
        **rollup_summary,
    }
    return checks, summary


def _housing_audit(
    schema: Mapping[str, Any],
    records_by_table: Mapping[str, list[dict[str, Any]]],
    repo_root: Path,
    token: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    from cfhe_data.airtable_sync import (  # pylint: disable=import-outside-toplevel
        _airtable_identity,
        _source_identity,
        _target_bands,
    )

    config = json.loads(
        (repo_root / "config" / "airtable_sync.json").read_text(encoding="utf-8")
    )
    cycle_policy = json.loads(
        (repo_root / "config" / "airtable_cycles.json").read_text(encoding="utf-8")
    )
    jurisdiction_table = config["jurisdictions_table_id"]
    rhna_table = config["rhna_table_id"]
    jf = config["jurisdiction_fields"]
    rf = config["rhna_fields"]
    jurisdiction_rows = records_by_table[jurisdiction_table]
    rhna_rows = records_by_table[rhna_table]
    excluded = set(config["excluded_jurisdictions"])
    _tables, schema_fields, _field_table, _views = _field_maps(schema)

    checks: list[dict[str, Any]] = []
    names = [
        record.get("fields", {}).get(jf["name"], "") for record in jurisdiction_rows
    ]
    duplicate_names = sum(
        count - 1
        for count in Counter(name.casefold() for name in names).values()
        if count > 1
    )
    checks.append(
        _check(
            "housing chain",
            "jurisdiction table coverage",
            "fail"
            if len(jurisdiction_rows) != config["expected_jurisdiction_record_count"]
            or duplicate_names
            else "pass",
            "high" if duplicate_names else "info",
            len(jurisdiction_rows),
            abs(len(jurisdiction_rows) - config["expected_jurisdiction_record_count"])
            + duplicate_names,
            (
                f"{len(jurisdiction_rows)} live jurisdiction records, "
                f"{duplicate_names} case-insensitive duplicate names"
            ),
        )
    )

    current_rows_by_city: dict[str, list[dict[str, Any]]] = defaultdict(list)
    correct_link_failures = 0
    formula_recompute_failures: Counter[str] = Counter()
    cycle_counts: Counter[str] = Counter()
    for row in rhna_rows:
        fields = row.get("fields", {})
        city_ids = fields.get(rf["jurisdiction_link"], []) or []
        if fields.get(rf["correct_link"]) not in (True, 1):
            correct_link_failures += 1
        if fields.get(rf["current"]) in (True, 1):
            for city_id in city_ids:
                current_rows_by_city[city_id].append(row)

        cycle_values = fields.get(rf["cycle"], []) or []
        if fields.get(rf["current"]) in (True, 1) and len(cycle_values) == 1:
            cycle_counts[str(cycle_values[0])] += 1

        vli = fields.get(rf["vli"], 0) or 0
        li = fields.get(rf["li"], 0) or 0
        mi = fields.get(rf["mi"], 0) or 0
        ami = fields.get(rf["ami"], 0) or 0
        override = fields.get(rf["total_progress_override"], 0) or 0
        expected_total = override if override != 0 else vli + li + mi + ami
        actual_total = fields.get("fldvW5QO6skRvpL0Q", 0) or 0
        if not _same_value(actual_total, expected_total):
            formula_recompute_failures["total progress"] += 1

        target_parts = [
            fields.get("fldFxNucslp94SBYq", 0) or 0,
            fields.get("fldtLexU98xpd9kc5", 0) or 0,
            fields.get("fldv65hKdBuwKJrAH", 0) or 0,
            fields.get("fldTzE6qJ6LEi3Mun", 0) or 0,
        ]
        target_override = fields.get("fldKMNyYcFc1nX6aS")
        expected_target = (
            sum(target_parts) if target_override in (None, "") else target_override
        )
        actual_target = fields.get("fldNtc1LNYxV8HHBm", 0) or 0
        if not _same_value(actual_target, expected_target):
            formula_recompute_failures["total target"] += 1
        if expected_target:
            expected_pct = expected_total / expected_target * 100
            actual_pct = fields.get("fldkAAI0FxvMhY8mv", 0) or 0
            if not _same_value(actual_pct, expected_pct):
                formula_recompute_failures["total progress percent"] += 1

        he_start = _date_value(fields.get("fldKnp4CZfTfqsoS2"))
        he_end = _date_value(fields.get("fldp4JB7zcN7kEixX"))
        today = dt.datetime.now(dt.UTC).date()
        expected_current = bool(
            he_start and he_end and he_start <= today and today < he_end
        )
        if bool(fields.get(rf["current"])) != expected_current:
            formula_recompute_failures["current RHNA"] += 1

    checks.append(
        _check(
            "housing chain",
            "RHNA arithmetic formulas recompute",
            "fail" if sum(formula_recompute_failures.values()) else "pass",
            "high" if sum(formula_recompute_failures.values()) else "info",
            len(rhna_rows),
            sum(formula_recompute_failures.values()),
            f"{sum(formula_recompute_failures.values())} mismatches in independently recomputed core RHNA formulas",
            affected_formulas=dict(formula_recompute_failures),
        )
    )
    checks.append(
        _check(
            "housing chain",
            "RHNA city and cycle links agree",
            "fail" if correct_link_failures else "pass",
            "high" if correct_link_failures else "info",
            len(rhna_rows),
            correct_link_failures,
            f"{correct_link_failures} RHNA rows have a false or blank Correct Link formula",
        )
    )

    target_cities = {
        record["id"]
        for record in jurisdiction_rows
        if record.get("fields", {}).get(jf["name"]) not in excluded
    }
    current_cardinality_failures = {
        city_id: len(current_rows_by_city.get(city_id, []))
        for city_id in target_cities
        if len(current_rows_by_city.get(city_id, [])) != 1
    }
    checks.append(
        _check(
            "housing chain",
            "one current RHNA row per published jurisdiction",
            "fail" if current_cardinality_failures else "pass",
            "high" if current_cardinality_failures else "info",
            len(target_cities),
            len(current_cardinality_failures),
            f"{len(current_cardinality_failures)} of {len(target_cities)} published jurisdictions lack exactly one current RHNA row",
        )
    )

    expected_cycles = Counter(cycle_policy["expected_cycle_counts"])
    cycle_failure = dict(cycle_counts) != dict(expected_cycles)
    checks.append(
        _check(
            "housing chain",
            "current cycle counts match policy",
            "fail" if cycle_failure else "pass",
            "high" if cycle_failure else "info",
            sum(cycle_counts.values()),
            1 if cycle_failure else 0,
            f"live current rows are {dict(sorted(cycle_counts.items()))}; policy expects {dict(sorted(expected_cycles.items()))}",
        )
    )

    jurisdiction_lookup_map = {
        "fldYnPVSHKkXwTQmR": "fldkAAI0FxvMhY8mv",
        "flddeMRE0zJU0z5z2": "fldz32T24Prq6BwXk",
        "fldNwN6gtSCFQm4Z6": "fldkQNi8rAhOof1HV",
    }
    passthrough_mismatches: Counter[str] = Counter()
    for city in jurisdiction_rows:
        if city["id"] not in target_cities:
            continue
        current = current_rows_by_city.get(city["id"], [])
        if len(current) != 1:
            continue
        current_fields = current[0].get("fields", {})
        city_fields = city.get("fields", {})
        for destination, source in jurisdiction_lookup_map.items():
            if not _same_value(
                city_fields.get(destination, []), current_fields.get(source)
            ):
                passthrough_mismatches[destination] += 1
    checks.append(
        _check(
            "housing chain",
            "current RHNA values pass through to Jurisdictions",
            "fail" if sum(passthrough_mismatches.values()) else "pass",
            "high" if sum(passthrough_mismatches.values()) else "info",
            len(target_cities) * len(jurisdiction_lookup_map),
            sum(passthrough_mismatches.values()),
            f"{sum(passthrough_mismatches.values())} current-cycle passthrough values differ",
            affected_fields=dict(passthrough_mismatches),
        )
    )

    current_he_by_city = current_rows_by_city
    current_he_cardinality = sum(
        1 for city_id in target_cities if len(current_he_by_city.get(city_id, [])) != 1
    )
    checks.append(
        _check(
            "housing chain",
            "one current housing element row per published jurisdiction",
            "fail" if current_he_cardinality else "pass",
            "high" if current_he_cardinality else "info",
            len(target_cities),
            current_he_cardinality,
            f"{current_he_cardinality} published jurisdictions lack exactly one current housing element row",
        )
    )

    derivative_contracts = {
        "fld1h7uuhM0O5S5ue": (
            "formula",
            {"fldKnp4CZfTfqsoS2", "fldp4JB7zcN7kEixX"},
        ),
        "fldMj3H3UwYUev6L5": (
            "formula",
            {"fld1h7uuhM0O5S5ue", "fldKnp4CZfTfqsoS2"},
        ),
        "fldII9NJ4Gctk6TzJ": (
            "formula",
            {"fld1h7uuhM0O5S5ue", "fldn4NPwTPf2r7yDo"},
        ),
        "fldaC9KKjI7j1cKDl": (
            "formula",
            {"fld1h7uuhM0O5S5ue", "fldShfN899CMV3EtG"},
        ),
    }
    contract_failures: list[dict[str, Any]] = []
    for field_id, (expected_type, expected_refs) in derivative_contracts.items():
        field = schema_fields.get(field_id, {})
        actual_refs = set(field.get("options", {}).get("referencedFieldIds", []))
        if field.get("type") != expected_type or actual_refs != expected_refs:
            contract_failures.append(
                {
                    "field_id": field_id,
                    "expected_type": expected_type,
                    "expected_references": sorted(expected_refs),
                }
            )

    rollup_contracts = {
        "fldj89LundQCtgrlO": "fldMj3H3UwYUev6L5",
        "fld3PqVCWyIdu1Um5": "fldII9NJ4Gctk6TzJ",
        "fldYR0pkTi8gUypPB": "fldaC9KKjI7j1cKDl",
    }
    for field_id, expected_source in rollup_contracts.items():
        field = schema_fields.get(field_id, {})
        options = field.get("options", {})
        if (
            field.get("type") != "rollup"
            or options.get("recordLinkFieldId") != "fld6xPtwlbVuxI8Wm"
            or options.get("fieldIdInLinkedTable") != expected_source
        ):
            contract_failures.append(
                {
                    "field_id": field_id,
                    "expected_type": "rollup",
                    "expected_link_field_id": "fld6xPtwlbVuxI8Wm",
                    "expected_source_field_id": expected_source,
                }
            )
    checks.append(
        _check(
            "housing chain",
            "current-cycle helper and jurisdiction rollup schema contracts",
            "fail" if contract_failures else "pass",
            "high" if contract_failures else "info",
            len(derivative_contracts) + len(rollup_contracts),
            len(contract_failures),
            f"{len(contract_failures)} current-cycle schema contracts differ",
            affected_fields=contract_failures,
        )
    )

    helper_map = {
        "fldMj3H3UwYUev6L5": "fldKnp4CZfTfqsoS2",
        "fldII9NJ4Gctk6TzJ": "fldn4NPwTPf2r7yDo",
        "fldaC9KKjI7j1cKDl": "fldShfN899CMV3EtG",
    }
    helper_mismatches: Counter[str] = Counter()
    for row in rhna_rows:
        row_fields = row.get("fields", {})
        is_current = row_fields.get(rf["current"]) in (True, 1)
        for helper_id, source_id in helper_map.items():
            expected = row_fields.get(source_id) if is_current else None
            if helper_id in {"fldMj3H3UwYUev6L5", "fldII9NJ4Gctk6TzJ"}:
                same = _date_value(row_fields.get(helper_id)) == _date_value(expected)
            else:
                same = _same_value(row_fields.get(helper_id), expected)
            if not same:
                helper_mismatches[helper_id] += 1
    helper_failures = sum(helper_mismatches.values())
    checks.append(
        _check(
            "housing chain",
            "current-cycle helper formulas select only authoritative RHNA rows",
            "fail" if helper_failures else "pass",
            "high" if helper_failures else "info",
            len(rhna_rows) * len(helper_map),
            helper_failures,
            f"{helper_failures} helper values differ from Current-gated source values",
            affected_fields=dict(helper_mismatches),
        )
    )

    he_lookup_map = {
        "fldYR0pkTi8gUypPB": "fldShfN899CMV3EtG",
        "fld3PqVCWyIdu1Um5": "fldn4NPwTPf2r7yDo",
        "fldj89LundQCtgrlO": "fldKnp4CZfTfqsoS2",
    }
    he_passthrough_mismatches: Counter[str] = Counter()
    compliance_counts: Counter[str] = Counter()
    for city in jurisdiction_rows:
        current = current_he_by_city.get(city["id"], [])
        city_fields = city.get("fields", {})
        current_fields = current[0].get("fields", {}) if len(current) == 1 else {}
        for destination, source in he_lookup_map.items():
            if not _same_value(
                city_fields.get(destination, []), current_fields.get(source)
            ):
                he_passthrough_mismatches[destination] += 1
        compliance = _first(city_fields.get("fldYR0pkTi8gUypPB"))
        compliance_counts[str(compliance) if compliance is not None else "blank"] += 1
    checks.append(
        _check(
            "housing chain",
            "current housing element values pass through to Jurisdictions",
            "fail" if sum(he_passthrough_mismatches.values()) else "pass",
            "high" if sum(he_passthrough_mismatches.values()) else "info",
            len(jurisdiction_rows) * len(he_lookup_map),
            sum(he_passthrough_mismatches.values()),
            f"{sum(he_passthrough_mismatches.values())} current housing element passthrough values differ",
            affected_fields=dict(he_passthrough_mismatches),
        )
    )

    builders_remedy_mismatches = 0
    builders_remedy_flag_mismatches = 0
    builders_remedy_expected: Counter[str] = Counter()
    builders_remedy_actual: Counter[str] = Counter()
    today = dt.datetime.now(dt.UTC).date()
    for city in jurisdiction_rows:
        city_fields = city.get("fields", {})

        compliance_values = _flatten(city_fields.get("fldYR0pkTi8gUypPB"))
        start_values = _flatten(city_fields.get("fldj89LundQCtgrlO"))
        review_values = _flatten(city_fields.get("fld3PqVCWyIdu1Um5"))
        compliance = str(compliance_values[0]) if compliance_values else ""
        start = (
            dt.date.fromisoformat(str(start_values[0])[:10]) if start_values else None
        )
        reviewed = (
            dt.date.fromisoformat(str(review_values[0])[:10]) if review_values else None
        )

        if compliance == "Out of Compliance" and start and start < today:
            expected = "Applies"
        elif (
            compliance == "In Compliance"
            and start
            and reviewed
            and reviewed > start + dt.timedelta(days=1)
        ):
            expected = f"Applied until {reviewed:%m/%d/%y}"
        else:
            expected = "Does not apply"
        actual = str(city_fields.get("fld5Q7scnodRiwqfR", ""))
        expected_class = (
            "Applied until" if expected.startswith("Applied until ") else expected
        )
        actual_class = (
            "Applied until" if actual.startswith("Applied until ") else actual
        )
        builders_remedy_expected[expected_class] += 1
        builders_remedy_actual[actual_class] += 1
        if actual != expected:
            builders_remedy_mismatches += 1
        expected_flag = (
            "Applies"
            if expected == "Applies"
            else (
                "Used to apply"
                if expected_class == "Applied until"
                else "Does not apply"
            )
        )
        if str(city_fields.get("fld32WiiGMW4N3IRP", "")) != expected_flag:
            builders_remedy_flag_mismatches += 1

    checks.append(
        _check(
            "housing chain",
            "Builder's Remedy status matches current housing element data",
            "fail"
            if builders_remedy_mismatches or builders_remedy_flag_mismatches
            else "pass",
            "high"
            if builders_remedy_mismatches or builders_remedy_flag_mismatches
            else "info",
            len(jurisdiction_rows) * 2,
            builders_remedy_mismatches + builders_remedy_flag_mismatches,
            (
                f"{builders_remedy_mismatches} text and "
                f"{builders_remedy_flag_mismatches} flag values differ across "
                f"{len(jurisdiction_rows)} jurisdictions"
            ),
            expected_status_counts=dict(builders_remedy_expected),
            actual_status_counts=dict(builders_remedy_actual),
            text_mismatch_count=builders_remedy_mismatches,
            flag_mismatch_count=builders_remedy_flag_mismatches,
        )
    )

    census_records = records_by_table.get("tblR7D5S79OxWmypk", [])
    census_map = {record["id"]: record for record in census_records}
    census_groups: dict[tuple[tuple[str, ...], str], list[dict[str, Any]]] = (
        defaultdict(list)
    )
    census_link_violations = 0
    for record in census_records:
        fields = record.get("fields", {})
        city_links = tuple(sorted(fields.get("fldlBqkFnB8Nk7oU6", []) or []))
        if len(city_links) != 1:
            census_link_violations += 1
        year_key = _canonical(fields.get("fldCCRD0q1Gq6LZA9"))
        census_groups[(city_links, year_key)].append(record)
    duplicate_census_groups = [
        group for group in census_groups.values() if len(group) > 1
    ]
    duplicate_census_excess = sum(len(group) - 1 for group in duplicate_census_groups)
    census_metric_ids = [
        "fld8FyP40IyAdy4OZ",
        "fldIPhVVQ0eoP66Cv",
        "fldWTanzpOAhhNL3w",
        "fldditDLqkkm4BOAv",
    ]
    conflicting_census_groups = sum(
        1
        for group in duplicate_census_groups
        if len(
            {
                tuple(
                    _canonical(record.get("fields", {}).get(field_id))
                    for field_id in census_metric_ids
                )
                for record in group
            }
        )
        > 1
    )
    census_key_failures = census_link_violations + duplicate_census_excess
    checks.append(
        _check(
            "housing chain",
            "Census record keys are unique and linked to one jurisdiction",
            "fail" if census_key_failures else "pass",
            "medium" if census_key_failures else "info",
            len(census_records),
            census_key_failures,
            (
                f"{census_link_violations} rows lack exactly one jurisdiction; "
                f"{len(duplicate_census_groups)} duplicate jurisdiction-year groups contain "
                f"{duplicate_census_excess} excess rows, including "
                f"{conflicting_census_groups} groups with conflicting metrics"
            ),
            one_jurisdiction_link_violations=census_link_violations,
            duplicate_group_count=len(duplicate_census_groups),
            duplicate_excess_row_count=duplicate_census_excess,
            conflicting_metric_group_count=conflicting_census_groups,
        )
    )
    census_lookup_map = {
        "fldNwU8IziwAtCr37": "fld8FyP40IyAdy4OZ",
        "fldGWoXXPkivxKNXV": "fldditDLqkkm4BOAv",
        "fldxluQSDBcvs2y9b": "fldIPhVVQ0eoP66Cv",
        "fldYI1ZxexFVELZ7Q": "fldbHaRZd7UqhAopq",
        "fldPr3YcT1lxG9HET": "fldsPi06AevSVrx0M",
    }
    census_passthrough_mismatches: Counter[str] = Counter()
    census_values_checked = 0
    for city in jurisdiction_rows:
        if city["id"] not in target_cities:
            continue
        linked = [
            census_map[record_id]
            for record_id in city.get("fields", {}).get("fldalVzk7MDohG8nv", []) or []
            if record_id in census_map
        ]
        if not linked:
            continue

        def census_year(record: Mapping[str, Any]) -> int:
            values = _flatten(record.get("fields", {}).get("fldCCRD0q1Gq6LZA9"))
            match = re.search(r"\d{4}", str(values[0])) if values else None
            return int(match.group()) if match else -1

        latest_year = max(census_year(record) for record in linked)
        latest = [record for record in linked if census_year(record) == latest_year]
        city_fields = city.get("fields", {})
        for destination, source in census_lookup_map.items():
            expected = _source_values(
                {record["id"]: record for record in latest},
                [record["id"] for record in latest],
                source,
            )
            census_values_checked += 1
            if not _same_value(city_fields.get(destination, []), expected):
                census_passthrough_mismatches[destination] += 1
    checks.append(
        _check(
            "housing chain",
            "latest census values pass through to Jurisdictions",
            "fail" if sum(census_passthrough_mismatches.values()) else "pass",
            "high" if sum(census_passthrough_mismatches.values()) else "info",
            census_values_checked,
            sum(census_passthrough_mismatches.values()),
            f"{sum(census_passthrough_mismatches.values())} latest-census passthrough values differ",
            affected_fields=dict(census_passthrough_mismatches),
        )
    )

    totals = json.loads(
        (repo_root / "data" / "processed" / "airtable_totals.json").read_text(
            encoding="utf-8"
        )
    )
    source_by_identity = {_source_identity(row): row for row in totals["jurisdictions"]}
    blank_cells_by_band: Counter[str] = Counter()
    blank_record_count = 0
    exact_value_mismatches = 0
    for city in jurisdiction_rows:
        fields = city.get("fields", {})
        name = fields.get(jf["name"], "")
        if name in excluded:
            continue
        identity = _airtable_identity(
            name, bool(fields.get(jf["unincorporated"], False))
        )
        source_row = source_by_identity.get(identity)
        current = current_rows_by_city.get(city["id"], [])
        if source_row is None or len(current) != 1:
            continue
        desired = _target_bands(source_row)
        live_fields = current[0].get("fields", {})
        record_has_blank = False
        for band, expected in desired.items():
            field_id = rf[band]
            if field_id not in live_fields or live_fields.get(field_id) is None:
                if expected == 0:
                    blank_cells_by_band[band] += 1
                    record_has_blank = True
                else:
                    exact_value_mismatches += 1
            elif live_fields[field_id] != expected:
                exact_value_mismatches += 1
        if record_has_blank:
            blank_record_count += 1
    checks.append(
        _check(
            "housing chain",
            "managed permit cells equal reviewed source without blank coercion",
            "fail"
            if exact_value_mismatches
            else ("warn" if blank_record_count else "pass"),
            "high"
            if exact_value_mismatches
            else ("medium" if blank_record_count else "info"),
            len(target_cities) * 4,
            exact_value_mismatches + sum(blank_cells_by_band.values()),
            (
                f"{exact_value_mismatches} numerical mismatches; {sum(blank_cells_by_band.values())} "
                f"blank cells across {blank_record_count} records are coerced to source zero"
            ),
            blank_cells_by_band=dict(blank_cells_by_band),
            blank_record_count=blank_record_count,
        )
    )

    desired_total = sum(row["total"] for row in totals["jurisdictions"])
    live_total = 0
    for rows in current_rows_by_city.values():
        if len(rows) != 1:
            continue
        fields = rows[0].get("fields", {})
        live_total += sum(
            (fields.get(rf[key], 0) or 0) for key in ("vli", "li", "mi", "ami")
        )
    checks.append(
        _check(
            "housing chain",
            "live managed permit total equals reviewed source",
            "fail" if live_total != desired_total else "pass",
            "high" if live_total != desired_total else "info",
            len(target_cities),
            abs(live_total - desired_total),
            f"live managed permits total {live_total:,}; reviewed source total {desired_total:,}",
        )
    )

    env = os.environ.copy()
    env["AIRTABLE_TOKEN"] = token
    sync_proc = subprocess.run(
        [
            "cfhe-data",
            "airtable-sync",
            "--totals",
            "data/processed/airtable_totals.json",
            "--config",
            "config/airtable_sync.json",
            "--git-sha",
            subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo_root,
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip(),
        ],
        cwd=repo_root,
        text=True,
        capture_output=True,
        check=False,
        timeout=240,
        env=env,
    )
    sync_summary: dict[str, Any] = {}
    if sync_proc.returncode == 0:
        try:
            sync_summary = json.loads(sync_proc.stdout)
        except ValueError:
            sync_summary = {"raw_summary": sync_proc.stdout[-1000:]}
    change_count = sync_summary.get("change_count")
    dry_run_failure = sync_proc.returncode != 0 or change_count not in (0, None)
    checks.append(
        _check(
            "housing chain",
            "production sync dry run is idempotent",
            "fail" if dry_run_failure else "pass",
            "high" if dry_run_failure else "info",
            len(target_cities),
            int(change_count or 0) if sync_proc.returncode == 0 else 1,
            (
                f"dry run exited {sync_proc.returncode}; "
                f"change_count={change_count}; unchanged_count={sync_summary.get('unchanged_count')}"
            ),
        )
    )

    return checks, {
        "current_cycle_counts": dict(sorted(cycle_counts.items())),
        "current_rhna_count": sum(cycle_counts.values()),
        "jurisdiction_current_cardinality": dict(
            sorted(
                Counter(
                    str(len(current_rows_by_city.get(city["id"], [])))
                    for city in jurisdiction_rows
                ).items()
            )
        ),
        "current_helper_mismatch_count": helper_failures,
        "he_passthrough_mismatch_count": sum(he_passthrough_mismatches.values()),
        "current_compliance_counts": dict(sorted(compliance_counts.items())),
        "builder_text_mismatch_count": builders_remedy_mismatches,
        "builder_flag_mismatch_count": builders_remedy_flag_mismatches,
        "builder_class_counts": dict(sorted(builders_remedy_expected.items())),
        "census_record_count": len(census_records),
        "census_link_violation_count": census_link_violations,
        "census_duplicate_group_count": len(duplicate_census_groups),
        "census_duplicate_excess_row_count": duplicate_census_excess,
        "desired_permit_total": desired_total,
        "live_permit_total": live_total,
        "sync_summary": sync_summary,
    }


def _targeted_formula_audit(
    schema: Mapping[str, Any],
    records_by_table: Mapping[str, list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Independently recompute repaired formula chains with zero-safe guards."""

    _tables, fields, _field_table, _views = _field_maps(schema)
    checks: list[dict[str, Any]] = []
    formula_contracts = {
        "fldUn4QqM2XO1h5go": ({"fldYI1ZxexFVELZ7Q"}, "ARRAYJOIN"),
        "fldwn19zTAv18Wd9Z": ({"fldYI1ZxexFVELZ7Q"}, "ARRAYJOIN"),
        "fldkQNi8rAhOof1HV": (
            {"fldJ5AoqSqy35sBjQ", "fldKuBCbXc8PKSOs3", "fldNtc1LNYxV8HHBm"},
            "ARRAYJOIN",
        ),
        "fld12rtdGBMFItOGJ": (
            {"fldCxvtdH0kbbkP6V", "fldoC8pqolQyEPxPm"},
            ">0",
        ),
    }
    contract_failures: list[dict[str, Any]] = []
    for field_id, (expected_refs, required_guard) in formula_contracts.items():
        field = fields.get(field_id, {})
        options = field.get("options", {})
        compact_formula = re.sub(r"\s+", "", str(options.get("formula", "")))
        if (
            field.get("type") != "formula"
            or set(options.get("referencedFieldIds", [])) != expected_refs
            or required_guard not in compact_formula
        ):
            contract_failures.append(
                {
                    "field_id": field_id,
                    "expected_references": sorted(expected_refs),
                    "required_guard": required_guard,
                }
            )
    checks.append(
        _check(
            "formulas",
            "zero-safe formula schema contracts",
            "fail" if contract_failures else "pass",
            "high" if contract_failures else "info",
            len(formula_contracts),
            len(contract_failures),
            f"{len(contract_failures)} repaired formula schema contracts differ",
            affected_fields=contract_failures,
        )
    )

    rent_overview_mismatches = 0
    rent_text_mismatches = 0
    rent_classes: Counter[str] = Counter()
    for record in records_by_table[JURISDICTIONS_TABLE]:
        row = record.get("fields", {})
        rent = _number_value(row.get("fldYI1ZxexFVELZ7Q"))
        if rent is None:
            expected_overview = None
            expected_text = None
            rent_classes["blank"] += 1
        else:
            expected_overview = "Negative" if rent > 0.29 else "Positive"
            rent_classes[expected_overview] += 1
            percent = _airtable_round(rent * 100)
            expected_text = (
                f"{percent}% of renters in this jurisdiction spend 2/3 or more of their "
                "income on rent."
                if rent > 0.29
                else (
                    f"{percent}% of renters spend 2/3 or more of their income on rent - "
                    "the California average is 30%."
                )
            )
        if not _same_value(row.get("fldUn4QqM2XO1h5go"), expected_overview):
            rent_overview_mismatches += 1
        if not _same_value(row.get("fldwn19zTAv18Wd9Z"), expected_text):
            rent_text_mismatches += 1
    rent_failures = rent_overview_mismatches + rent_text_mismatches
    checks.append(
        _check(
            "formulas",
            "Rent Burden derivatives preserve numeric zero and recompute exactly",
            "fail" if rent_failures else "pass",
            "high" if rent_failures else "info",
            len(records_by_table[JURISDICTIONS_TABLE]) * 2,
            rent_failures,
            (
                f"{rent_overview_mismatches} overview and {rent_text_mismatches} text "
                "values differ from the zero-safe recomputation"
            ),
            class_counts=dict(sorted(rent_classes.items())),
        )
    )

    predictions_by_record: dict[str, str] = {}
    prediction_mismatches = 0
    prediction_classes: Counter[str] = Counter()
    current_insufficient = 0
    for record in records_by_table[RHNA_TABLE]:
        row = record.get("fields", {})
        prior_percent = _number_value(row.get("fldJ5AoqSqy35sBjQ"))
        prior_total = _number_value(row.get("fldKuBCbXc8PKSOs3"))
        target = _number_value(row.get("fldNtc1LNYxV8HHBm"))
        if prior_percent is None:
            expected = INSUFFICIENT_PREDICTION
            prediction_class = "insufficient"
        elif prior_percent > 100:
            if prior_total is None or target is None or target == 0:
                expected = INSUFFICIENT_PREDICTION
                prediction_class = "insufficient"
            elif prior_total > target:
                expected = "it will meet its RHNA targets."
                prediction_class = "meet"
            else:
                expected = (
                    f"it will only meet {_airtable_round(prior_total / target * 100)}% "
                    "of the identified need."
                )
                prediction_class = "partial_total"
        else:
            expected = f"it will only meet {_airtable_round(prior_percent)}% of the identified need."
            prediction_class = "partial_percent"
        predictions_by_record[record["id"]] = expected
        prediction_classes[prediction_class] += 1
        if (
            row.get("fld1h7uuhM0O5S5ue") in (True, 1)
            and prediction_class == "insufficient"
        ):
            current_insufficient += 1
        if str(row.get("fldkQNi8rAhOof1HV", "")) != expected:
            prediction_mismatches += 1

    rhna_map = {record["id"]: record for record in records_by_table[RHNA_TABLE]}
    prediction_lookup_mismatches = 0
    prediction_color_mismatches = 0
    for record in records_by_table[JURISDICTIONS_TABLE]:
        row = record.get("fields", {})
        current_ids = [
            record_id
            for record_id in row.get("fld6xPtwlbVuxI8Wm", []) or []
            if record_id in rhna_map
            and rhna_map[record_id].get("fields", {}).get("fld1h7uuhM0O5S5ue")
            in (True, 1)
        ]
        expected_values = [
            predictions_by_record[record_id] for record_id in current_ids
        ]
        actual_lookup = row.get("fldNwN6gtSCFQm4Z6", [])
        if not _same_value(actual_lookup, expected_values):
            prediction_lookup_mismatches += 1
        joined_lookup = " ".join(str(item) for item in _flatten(actual_lookup))
        if joined_lookup == "it will meet its RHNA targets.":
            expected_color = "#1570EF"
        elif "not enough prior-cycle data" in joined_lookup:
            expected_color = "#111927"
        else:
            expected_color = "#ED3740"
        if str(row.get("fld9BarCCff2CNHZP", "")) != expected_color:
            prediction_color_mismatches += 1
    prediction_failures = (
        prediction_mismatches
        + prediction_lookup_mismatches
        + prediction_color_mismatches
    )
    checks.append(
        _check(
            "formulas",
            "RHNA Prediction, current passthrough, and color recompute exactly",
            "fail" if prediction_failures else "pass",
            "high" if prediction_failures else "info",
            len(records_by_table[RHNA_TABLE])
            + len(records_by_table[JURISDICTIONS_TABLE]) * 2,
            prediction_failures,
            (
                f"{prediction_mismatches} Prediction, {prediction_lookup_mismatches} "
                f"lookup, and {prediction_color_mismatches} color values differ"
            ),
            prediction_class_counts=dict(sorted(prediction_classes.items())),
            current_insufficient_prediction_count=current_insufficient,
        )
    )

    pro_housing_mismatches = 0
    pro_housing_errors = 0
    denominator_counts: Counter[str] = Counter()
    for record in records_by_table[REPORTS_TABLE]:
        row = record.get("fields", {})
        denominator = _number_value(row.get("fldoC8pqolQyEPxPm"))
        numerator = _number_value(row.get("fldCxvtdH0kbbkP6V"))
        if denominator is None:
            denominator_counts["blank"] += 1
            expected = None
        elif denominator <= 0:
            denominator_counts["zero_or_negative"] += 1
            expected = None
        else:
            denominator_counts["positive"] += 1
            expected = (numerator or 0) / denominator * 100
        actual = row.get("fld12rtdGBMFItOGJ")
        if actual is not None and ERROR_PATTERN.search(str(actual)):
            pro_housing_errors += 1
        if not _same_value(actual, expected):
            pro_housing_mismatches += 1
    checks.append(
        _check(
            "formulas",
            "Reports Pro Housing percentage uses the exact guarded denominator",
            "fail" if pro_housing_mismatches or pro_housing_errors else "pass",
            "high" if pro_housing_mismatches or pro_housing_errors else "info",
            len(records_by_table[REPORTS_TABLE]),
            pro_housing_mismatches + pro_housing_errors,
            (
                f"{pro_housing_mismatches} exact ratios differ and "
                f"{pro_housing_errors} values contain errors"
            ),
            denominator_counts=dict(sorted(denominator_counts.items())),
        )
    )
    return checks, {
        "rent_overview_mismatch_count": rent_overview_mismatches,
        "rent_text_mismatch_count": rent_text_mismatches,
        "rent_class_counts": dict(sorted(rent_classes.items())),
        "rhna_prediction_mismatch_count": prediction_mismatches,
        "prediction_lookup_mismatch_count": prediction_lookup_mismatches,
        "prediction_color_mismatch_count": prediction_color_mismatches,
        "prediction_class_counts": dict(sorted(prediction_classes.items())),
        "current_insufficient_prediction_count": current_insufficient,
        "pro_housing_mismatch_count": pro_housing_mismatches,
        "pro_housing_error_count": pro_housing_errors,
        "pro_housing_denominator_counts": dict(sorted(denominator_counts.items())),
    }


def _event_email_audit(
    schema: Mapping[str, Any],
    records_by_table: Mapping[str, list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Validate Event email passthroughs by exact sets and duplicate-token counts."""

    _tables, fields, _field_table, _views = _field_maps(schema)
    checks: list[dict[str, Any]] = []
    event_lookup = fields.get("fldZAIGS3NQq0ZzWF", {})
    event_lookup_options = event_lookup.get("options", {})
    unique_formula = fields.get("fldRoG4MzJZOX4AbO", {})
    unique_options = unique_formula.get("options", {})
    relevant_schema = _canonical([event_lookup, unique_formula]).replace(" ", "")
    schema_failures = int(
        event_lookup.get("type") not in {"lookup", "multipleLookupValues"}
        or event_lookup_options.get("recordLinkFieldId") != "fldNNheVEGR64MVzC"
        or event_lookup_options.get("fieldIdInLinkedTable") != "fldXTdtatFJtrYiHt"
        or unique_formula.get("type") != "formula"
        or set(unique_options.get("referencedFieldIds", [])) != {"fldZAIGS3NQq0ZzWF"}
        or "ARRAYUNIQUE" not in str(unique_options.get("formula", ""))
        or "<75" in relevant_schema
    )
    checks.append(
        _check(
            "event email chain",
            "Event email lookup uses individual values without the legacy threshold",
            "fail" if schema_failures else "pass",
            "high" if schema_failures else "info",
            2,
            schema_failures,
            f"{schema_failures} Event email schema contract failures",
            destination_field_id="fldZAIGS3NQq0ZzWF",
            source_field_id="fldXTdtatFJtrYiHt",
            unique_formula_field_id="fldRoG4MzJZOX4AbO",
        )
    )

    watchdog_map = {
        record["id"]: record for record in records_by_table[WATCHDOGS_TABLE]
    }
    jurisdiction_map = {
        record["id"]: record for record in records_by_table[JURISDICTIONS_TABLE]
    }
    jurisdiction_email_sets: dict[str, set[str]] = {}
    jurisdiction_lookup_mismatches = 0
    for record_id, record in jurisdiction_map.items():
        row = record.get("fields", {})
        expected_tokens: list[str] = []
        for watchdog_id in row.get("fldOFHoM4FdWvRvRZ", []) or []:
            watchdog = watchdog_map.get(watchdog_id)
            if watchdog:
                expected_tokens.extend(
                    _email_tokens(watchdog.get("fields", {}).get("fldUCyiEtLSf2yI3b"))
                )
        expected_set = set(expected_tokens)
        jurisdiction_email_sets[record_id] = expected_set
        if set(_email_tokens(row.get("fldXTdtatFJtrYiHt"))) != expected_set:
            jurisdiction_lookup_mismatches += 1

    event_lookup_mismatches = 0
    event_unique_mismatches = 0
    exact_duplicate_events = 0
    normalized_duplicate_events = 0
    event_expected_counts: dict[str, int] = {}
    event_linked_jurisdictions: dict[str, list[str]] = {}
    for record in records_by_table[EVENTS_TABLE]:
        row = record.get("fields", {})
        linked_ids = row.get("fldNNheVEGR64MVzC", []) or []
        event_linked_jurisdictions[record["id"]] = linked_ids
        expected_set: set[str] = set()
        for record_id in linked_ids:
            expected_set.update(jurisdiction_email_sets.get(record_id, set()))
        event_expected_counts[record["id"]] = len(expected_set)
        lookup_set = set(_email_tokens(row.get("fldZAIGS3NQq0ZzWF")))
        unique_tokens = _email_tokens(row.get("fldRoG4MzJZOX4AbO"))
        unique_set = set(unique_tokens)
        if lookup_set != expected_set:
            event_lookup_mismatches += 1
        if unique_set != expected_set:
            event_unique_mismatches += 1
        if len(unique_tokens) != len(unique_set):
            exact_duplicate_events += 1
        if len({token.casefold() for token in unique_set}) != len(unique_set):
            normalized_duplicate_events += 1

    set_failures = (
        jurisdiction_lookup_mismatches
        + event_lookup_mismatches
        + event_unique_mismatches
    )
    checks.append(
        _check(
            "event email chain",
            "Jurisdiction and Event email passthroughs preserve exact source sets",
            "fail" if set_failures else "pass",
            "high" if set_failures else "info",
            len(jurisdiction_map) + len(records_by_table[EVENTS_TABLE]) * 2,
            set_failures,
            (
                f"{jurisdiction_lookup_mismatches} Jurisdiction lookup, "
                f"{event_lookup_mismatches} Event lookup, and "
                f"{event_unique_mismatches} Unique Emails sets differ"
            ),
        )
    )
    checks.append(
        _check(
            "event email chain",
            "Unique Emails contains no exact duplicate tokens",
            "fail" if exact_duplicate_events else "pass",
            "high" if exact_duplicate_events else "info",
            len(records_by_table[EVENTS_TABLE]),
            exact_duplicate_events,
            f"{exact_duplicate_events} Event records contain exact duplicate tokens",
        )
    )

    watchdog_tokens = [
        token
        for record in records_by_table[WATCHDOGS_TABLE]
        for token in _email_tokens(record.get("fields", {}).get("fldUCyiEtLSf2yI3b"))
    ]
    exact_source_counts = Counter(watchdog_tokens)
    normalized_source_counts = Counter(token.casefold() for token in watchdog_tokens)
    exact_source_groups = sum(count > 1 for count in exact_source_counts.values())
    exact_source_excess = sum(
        count - 1 for count in exact_source_counts.values() if count > 1
    )
    normalized_source_groups = sum(
        count > 1 for count in normalized_source_counts.values()
    )
    normalized_source_excess = sum(
        count - 1 for count in normalized_source_counts.values() if count > 1
    )
    checks.append(
        _check(
            "event email chain",
            "case-normalized email repeats are classified as source data",
            "warn" if normalized_duplicate_events else "pass",
            "low" if normalized_duplicate_events else "info",
            len(records_by_table[EVENTS_TABLE]),
            normalized_duplicate_events,
            (
                f"{normalized_duplicate_events} Events retain case-only repeats from the "
                "Watchdogs source; exact sets and exact-token uniqueness remain valid"
            ),
            classification="source normalization, not a derivative defect",
            source_exact_duplicate_group_count=exact_source_groups,
            source_exact_duplicate_excess=exact_source_excess,
            source_case_normalized_duplicate_group_count=normalized_source_groups,
            source_case_normalized_duplicate_excess=normalized_source_excess,
        )
    )

    max_jurisdiction_count = max(map(len, jurisdiction_email_sets.values()), default=0)
    max_jurisdictions = {
        record_id
        for record_id, values in jurisdiction_email_sets.items()
        if len(values) == max_jurisdiction_count
    }
    high_volume_event_counts = Counter(
        event_expected_counts[event_id]
        for event_id, linked_ids in event_linked_jurisdictions.items()
        if max_jurisdictions.intersection(linked_ids)
    )
    return checks, {
        "event_record_count": len(records_by_table[EVENTS_TABLE]),
        "jurisdiction_email_lookup_set_mismatch_count": jurisdiction_lookup_mismatches,
        "event_lookup_set_mismatch_count": event_lookup_mismatches,
        "event_unique_formula_set_mismatch_count": event_unique_mismatches,
        "event_unique_exact_duplicate_token_event_count": exact_duplicate_events,
        "event_unique_case_normalized_duplicate_token_event_count": normalized_duplicate_events,
        "max_jurisdiction_unique_email_count": max_jurisdiction_count,
        "events_linked_to_max_email_jurisdiction_count": sum(
            high_volume_event_counts.values()
        ),
        "high_volume_event_unique_count_distribution": dict(
            sorted(high_volume_event_counts.items())
        ),
        "watchdog_exact_duplicate_email_group_count": exact_source_groups,
        "watchdog_exact_duplicate_record_excess": exact_source_excess,
        "watchdog_case_normalized_duplicate_email_group_count": normalized_source_groups,
        "watchdog_case_normalized_duplicate_record_excess": normalized_source_excess,
    }


def _repo_artifact_audit(
    repo_root: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    processed = repo_root / "data" / "processed"
    totals = json.loads(
        (processed / "jurisdiction_totals.json").read_text(encoding="utf-8")
    )
    airtable_totals = json.loads(
        (processed / "airtable_totals.json").read_text(encoding="utf-8")
    )
    audit = json.loads((processed / "audit_summary.json").read_text(encoding="utf-8"))
    airtable_audit = json.loads(
        (processed / "airtable_audit_summary.json").read_text(encoding="utf-8")
    )
    manifest = json.loads(
        (processed / "source_manifest.json").read_text(encoding="utf-8")
    )
    cycle_policy = json.loads(
        (repo_root / "config" / "airtable_cycles.json").read_text(encoding="utf-8")
    )

    checks: list[dict[str, Any]] = []
    schema_targets = [
        ("jurisdiction_totals.json", totals, "jurisdiction_totals.json"),
        ("airtable_totals.json", airtable_totals, "airtable_totals.json"),
    ]
    schema_errors: list[str] = []
    for label, payload, schema_name in schema_targets:
        schema = json.loads(
            (repo_root / "schemas" / schema_name).read_text(encoding="utf-8")
        )
        for error in Draft202012Validator(schema).iter_errors(payload):
            schema_errors.append(
                f"{label}:{'/'.join(map(str, error.path))}:{error.message}"
            )
    checks.append(
        _check(
            "repository derivatives",
            "processed JSON matches schemas",
            "fail" if schema_errors else "pass",
            "high" if schema_errors else "info",
            len(schema_targets),
            len(schema_errors),
            f"{len(schema_errors)} JSON schema errors",
            sample_errors=schema_errors[:20],
        )
    )

    duplicate_keys = 0
    arithmetic_failures = 0
    for payload in (totals, airtable_totals):
        keys = [row["jurisdiction_key"] for row in payload["jurisdictions"]]
        duplicate_keys += len(keys) - len(set(keys))
        for row in payload["jurisdictions"]:
            expected = sum(
                row[key]
                for key in (
                    "acutely_low",
                    "extremely_low",
                    "very_low",
                    "low",
                    "moderate",
                    "above_moderate",
                )
            )
            if row["total"] != expected:
                arithmetic_failures += 1
    checks.append(
        _check(
            "repository derivatives",
            "jurisdiction keys and row totals are unique and additive",
            "fail" if duplicate_keys or arithmetic_failures else "pass",
            "high" if duplicate_keys or arithmetic_failures else "info",
            len(totals["jurisdictions"]) + len(airtable_totals["jurisdictions"]),
            duplicate_keys + arithmetic_failures,
            f"{duplicate_keys} duplicate jurisdiction keys and {arithmetic_failures} nonadditive totals",
        )
    )

    invariant_failures = [
        f"audit_summary.{name}"
        for name, value in audit["invariants"].items()
        if value is not True
    ] + [
        f"airtable_audit_summary.{name}"
        for name, value in airtable_audit["invariants"].items()
        if value is not True
    ]
    checks.append(
        _check(
            "repository derivatives",
            "build conservation invariants pass",
            "fail" if invariant_failures else "pass",
            "high" if invariant_failures else "info",
            len(audit["invariants"]) + len(airtable_audit["invariants"]),
            len(invariant_failures),
            f"{len(invariant_failures)} failed conservation invariants",
            affected_invariants=invariant_failures,
        )
    )

    csv_path = processed / "jurisdiction_totals.csv"
    with csv_path.open(encoding="utf-8-sig", newline="") as handle:
        csv_rows = list(csv.DictReader(handle))
    csv_keys = {row["Jurisdiction"].casefold() for row in csv_rows}
    json_names = {row["jurisdiction"].casefold() for row in totals["jurisdictions"]}
    csv_failure = (
        len(csv_rows) != len(totals["jurisdictions"]) or csv_keys != json_names
    )
    checks.append(
        _check(
            "repository derivatives",
            "CSV and canonical JSON cover the same jurisdictions",
            "fail" if csv_failure else "pass",
            "high" if csv_failure else "info",
            len(csv_rows),
            len(csv_keys.symmetric_difference(json_names)),
            f"CSV has {len(csv_rows)} rows; JSON has {len(totals['jurisdictions'])} rows",
        )
    )

    expected_manifest_hash = _canonical_sha256(manifest)
    expected_cycle_hash = _canonical_sha256(cycle_policy)
    digest_failures = 0
    for payload in (totals, airtable_totals, audit, airtable_audit):
        metadata = payload["metadata"]
        if metadata["source_manifest_sha256"] != expected_manifest_hash:
            digest_failures += 1
    if airtable_totals["metadata"]["cycle_policy_sha256"] != expected_cycle_hash:
        digest_failures += 1
    if airtable_audit["metadata"]["cycle_policy_sha256"] != expected_cycle_hash:
        digest_failures += 1
    checks.append(
        _check(
            "repository derivatives",
            "source and cycle digests bind derivative files",
            "fail" if digest_failures else "pass",
            "high" if digest_failures else "info",
            6,
            digest_failures,
            f"{digest_failures} derivative digest references do not match canonical inputs",
        )
    )

    metadata_dates = {
        payload["metadata"]["last_updated"]
        for payload in (totals, airtable_totals, audit, airtable_audit)
    }
    retrieved_dates = {
        str(source["retrieved_at"])[:10] for source in manifest["sources"].values()
    }
    freshness_failure = len(metadata_dates) != 1 or not metadata_dates.issubset(
        retrieved_dates
    )
    checks.append(
        _check(
            "repository derivatives",
            "last updated metadata agrees across derivatives",
            "fail" if freshness_failure else "pass",
            "medium" if freshness_failure else "info",
            4,
            1 if freshness_failure else 0,
            f"derivative dates={sorted(metadata_dates)}; source retrieval dates={sorted(retrieved_dates)}",
        )
    )

    ledger_summaries: dict[str, dict[str, int]] = {}
    missing_ledgers: list[str] = []
    for ledger_name in (
        "dedupe_decisions.jsonl",
        "airtable_dedupe_decisions.jsonl",
    ):
        ledger_path = repo_root / "data" / "run" / ledger_name
        if not ledger_path.exists():
            missing_ledgers.append(ledger_name)
            continue
        index: dict[int, tuple[str, int]] = {}
        removals: list[tuple[int, int, list[int]]] = []
        with ledger_path.open(encoding="utf-8") as handle:
            for line in handle:
                row = json.loads(line)
                source_index = int(row["source_record_index"])
                index[source_index] = (str(row["decision"]), int(row["report_year"]))
                if row["decision"] != "retained":
                    removals.append(
                        (
                            source_index,
                            int(row["retained_report_year"]),
                            [int(value) for value in row["retained_source_indices"]],
                        )
                    )

        stale_edges = 0
        stale_entries: set[int] = set()
        missing_reference_edges = 0
        year_mismatch_edges = 0
        year_mismatch_entries: set[int] = set()
        for source_index, declared_year, references in removals:
            for reference in references:
                target = index.get(reference)
                if target is None:
                    missing_reference_edges += 1
                    continue
                target_decision, target_year = target
                if target_decision != "retained":
                    stale_edges += 1
                    stale_entries.add(source_index)
                if target_year != declared_year:
                    year_mismatch_edges += 1
                    year_mismatch_entries.add(source_index)

        ledger_summaries[ledger_name] = {
            "removed_entry_count": len(removals),
            "stale_reference_edge_count": stale_edges,
            "stale_reference_entry_count": len(stale_entries),
            "missing_reference_edge_count": missing_reference_edges,
            "year_mismatch_edge_count": year_mismatch_edges,
            "year_mismatch_entry_count": len(year_mismatch_entries),
        }

    lineage_failures = sum(
        summary["stale_reference_edge_count"]
        + summary["missing_reference_edge_count"]
        + summary["year_mismatch_edge_count"]
        for summary in ledger_summaries.values()
    )
    lineage_status = (
        "warn"
        if missing_ledgers and not lineage_failures
        else ("fail" if lineage_failures else "pass")
    )
    checks.append(
        _check(
            "repository derivatives",
            "deduplication ledgers point directly to final retained rows",
            lineage_status,
            "medium" if lineage_status != "pass" else "info",
            sum(
                summary["removed_entry_count"] for summary in ledger_summaries.values()
            ),
            lineage_failures + len(missing_ledgers),
            (
                f"{lineage_failures} stale, missing, or year-inconsistent reference edges "
                f"across {len(ledger_summaries)} ledgers; {len(missing_ledgers)} ledgers unavailable"
            ),
            ledger_summaries=ledger_summaries,
            missing_ledgers=missing_ledgers,
        )
    )

    missing_rule_summary = "removed_by_rule" not in airtable_audit
    checks.append(
        _check(
            "repository derivatives",
            "mixed-cycle audit summary exposes removals by rule",
            "warn" if missing_rule_summary else "pass",
            "medium" if missing_rule_summary else "info",
            1,
            int(missing_rule_summary),
            (
                "airtable_audit_summary.json omits removed_by_rule"
                if missing_rule_summary
                else "airtable_audit_summary.json includes removed_by_rule"
            ),
        )
    )

    return checks, {
        "jurisdiction_total": sum(row["total"] for row in totals["jurisdictions"]),
        "airtable_total": sum(row["total"] for row in airtable_totals["jurisdictions"]),
        "last_updated": next(iter(metadata_dates))
        if len(metadata_dates) == 1
        else None,
        "jurisdiction_rows": len(totals["jurisdictions"]),
        "airtable_rows": len(airtable_totals["jurisdictions"]),
        "ledger_summaries": ledger_summaries,
    }


def _surface_audit(
    schema: Mapping[str, Any],
    token: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    tables, fields, _field_table, views = _field_maps(schema)
    checks: list[dict[str, Any]] = []
    access_issues: list[str] = []

    pages: dict[str, Any] = {}
    automations: dict[str, Any] = {}
    automation_details: list[dict[str, Any]] = []
    try:
        pages = _cli_json(token, "list-pages-for-base", {"baseId": BASE_ID})
    except Exception as exc:  # noqa: BLE001
        access_issues.append(f"interfaces: {exc}")
    try:
        automations = _cli_json(token, "list-automations", {"baseId": BASE_ID})
        automation_list = automations.get("automations", [])
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {
                executor.submit(
                    _cli_json,
                    token,
                    "get-automation",
                    {
                        "baseId": BASE_ID,
                        "automationId": automation["id"],
                        "includeDeployedVersion": True,
                    },
                ): automation
                for automation in automation_list
            }
            for future in as_completed(futures):
                automation = futures[future]
                try:
                    automation_details.append(future.result())
                except Exception as exc:  # noqa: BLE001
                    access_issues.append(f"automation {automation['id']}: {exc}")
    except Exception as exc:  # noqa: BLE001
        access_issues.append(f"automations: {exc}")

    known = set(tables) | set(fields) | set(views)
    page_refs = _walk_ids(pages)
    bad_page_refs = sorted(ref for ref in page_refs if ref not in known)
    page_count = sum(
        len(interface.get("pages", [])) for interface in pages.get("interfaces", [])
    )
    checks.append(
        _check(
            "interfaces",
            "interface table, field, and view references resolve",
            "fail" if bad_page_refs else ("warn" if not pages else "pass"),
            "high" if bad_page_refs else ("medium" if not pages else "info"),
            page_count,
            len(bad_page_refs),
            f"{len(bad_page_refs)} unresolved Airtable IDs across {page_count} interface pages",
            unresolved_ids=bad_page_refs,
        )
    )

    county_interface_id = "pbdEArsQQTX5GVysY"
    county_interface = next(
        (
            interface
            for interface in pages.get("interfaces", [])
            if interface.get("id") == county_interface_id
        ),
        None,
    )
    county_pages = county_interface.get("pages", []) if county_interface else []
    county_page_structure_failures = abs(len(county_pages) - 9)
    county_page_probe_failures = 0
    county_page_probe_successes = 0
    for page in county_pages:
        elements = [
            element
            for element in page.get("dashboardElements", [])
            if element.get("title") == "Cities out of Compliance"
        ]
        if len(elements) != 1 or elements[0].get("sourceTableId") != RHNA_TABLE:
            county_page_structure_failures += 1
            continue
        try:
            _cli_json(
                token,
                "list-records-for-page",
                {
                    "baseId": BASE_ID,
                    "interfaceId": county_interface_id,
                    "pageId": page["id"],
                    "elementId": elements[0]["id"],
                },
            )
            county_page_probe_successes += 1
        except Exception as exc:  # noqa: BLE001
            county_page_probe_failures += 1
            access_issues.append(f"County interface page {page.get('id')}: {exc}")
    county_interface_failures = (
        county_page_structure_failures + county_page_probe_failures
    )
    checks.append(
        _check(
            "interfaces",
            "Counties interface exposes nine functional compliance pages",
            "fail" if county_interface_failures else "pass",
            "high" if county_interface_failures else "info",
            len(county_pages),
            county_interface_failures,
            (
                f"{len(county_pages)} Counties pages; {county_page_probe_successes} "
                f"compliance-list probes succeeded and {county_interface_failures} "
                "page structure or query checks failed"
            ),
            interface_id=county_interface_id,
            expected_page_count=9,
            page_probe_success_count=county_page_probe_successes,
        )
    )

    automation_list = automations.get("automations", [])
    invalid_config = [
        automation
        for automation in automation_list
        if automation.get("configurationStatus") != "valid"
    ]
    automation_refs = _walk_ids(automation_details or automations)
    bad_automation_refs = sorted(ref for ref in automation_refs if ref not in known)
    automation_failures = len(invalid_config) + len(bad_automation_refs)
    checks.append(
        _check(
            "automations",
            "automation configurations and Airtable references resolve",
            "fail" if automation_failures else ("warn" if not automations else "pass"),
            "high"
            if automation_failures
            else ("medium" if not automations else "info"),
            len(automation_list),
            automation_failures,
            (
                f"{len(invalid_config)} invalid configurations and "
                f"{len(bad_automation_refs)} unresolved Airtable IDs"
            ),
            unresolved_ids=bad_automation_refs,
        )
    )

    deployed = sum(
        1 for item in automation_list if item.get("deploymentStatus") == "deployed"
    )
    undeployed = len(automation_list) - deployed
    checks.append(
        _check(
            "automations",
            "automation deployment inventory",
            "warn" if undeployed else "pass",
            "low" if undeployed else "info",
            len(automation_list),
            undeployed,
            f"{deployed} deployed and {undeployed} undeployed automations",
            undeployed_names=[
                item.get("name")
                for item in automation_list
                if item.get("deploymentStatus") != "deployed"
            ],
        )
    )

    if access_issues:
        checks.append(
            _check(
                "access",
                "all downstream configurations were inspectable",
                "warn",
                "medium",
                2 + len(automation_list),
                len(access_issues),
                f"{len(access_issues)} configuration reads were unavailable",
                access_issues=access_issues,
            )
        )
    return checks, {
        "interface_count": len(pages.get("interfaces", [])),
        "interface_page_count": page_count,
        "county_interface_page_count": len(county_pages),
        "county_interface_probe_success_count": county_page_probe_successes,
        "standalone_form_count": len(pages.get("standaloneForms", [])),
        "automation_count": len(automation_list),
        "deployed_automation_count": deployed,
        "access_issues": access_issues,
    }


def run_audit(repo_root: Path | None = None) -> dict[str, Any]:
    """Run the complete read-only audit and return aggregate findings."""

    root = _repo_root(repo_root)
    token = _load_token(root)
    reader = AirtableReader(token)
    schema = reader.schema()
    records_by_table = {
        table["id"]: reader.records(table["id"]) for table in schema["tables"]
    }

    dependency_checks, dependency_summary = _dependency_audit(schema, records_by_table)
    housing_checks, housing_summary = _housing_audit(
        schema, records_by_table, root, token
    )
    formula_checks, formula_summary = _targeted_formula_audit(schema, records_by_table)
    event_checks, event_summary = _event_email_audit(schema, records_by_table)
    repo_checks, repo_summary = _repo_artifact_audit(root)
    surface_checks, surface_summary = _surface_audit(schema, token)
    checks = (
        dependency_checks
        + housing_checks
        + formula_checks
        + event_checks
        + repo_checks
        + surface_checks
    )

    status_counts = Counter(check["status"] for check in checks)
    severity_counts = Counter(
        check["severity"] for check in checks if check["status"] in {"fail", "warn"}
    )
    table_counts = {
        table["name"]: len(records_by_table[table["id"]]) for table in schema["tables"]
    }
    result = {
        "audit_version": 2,
        "base_id": BASE_ID,
        "executed_at": dt.datetime.now(dt.UTC).isoformat(),
        "read_only": True,
        "summary": {
            "overall_status": "fail"
            if status_counts["fail"]
            else ("warn" if status_counts["warn"] else "pass"),
            "check_counts": dict(sorted(status_counts.items())),
            "issue_severity_counts": dict(sorted(severity_counts.items())),
            "table_record_counts": table_counts,
            **dependency_summary,
            **housing_summary,
            **formula_summary,
            **event_summary,
            **repo_summary,
            **surface_summary,
        },
        "checks": checks,
    }
    output_dir = root / "data" / "airtable"
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "derivatives_audit_results.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return result


if __name__ == "__main__":
    audit = run_audit()
    print(
        json.dumps(
            {
                "overall_status": audit["summary"]["overall_status"],
                "check_counts": audit["summary"]["check_counts"],
                "issue_severity_counts": audit["summary"]["issue_severity_counts"],
                "executed_at": audit["executed_at"],
            },
            indent=2,
        )
    )
