# visibility: public
"""HCD housing element compliance source and guarded Airtable synchronization.

HCD's weekly Housing Element Compliance Report is the only source for the
`HE Compliance` and `Review Date` fields on each jurisdiction's current RHNA &
HE record. The Builder's Remedy and SB 423 formulas read those two fields, so
keeping them current keeps the public Builder's Remedy view current.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import re
import unicodedata
import urllib.request
from collections import Counter
from collections.abc import Callable, Mapping, Sequence

from .airtable_api import AirtableClient, RecordUpdate
from .airtable_sync import (
    _UNINCORPORATED_SUFFIX_RE,
    AirtableSyncBlockedError,
    AirtableSyncConfigurationError,
    AirtableSyncCoverageError,
    AirtableSyncSnapshotError,
    AirtableSyncStateChangedError,
    _complete_export,
    _cycle,
    _formula_boolean,
    _links,
    _optional_date,
    _verify_schema,
)
from .webflow import canonical_sha256

CKAN_PACKAGE_URL = "https://data.ca.gov/api/3/action/package_show?id={package_id}"
SOURCE_HEADER = (
    "Jurisdiction",
    "County",
    "Planning Period",
    "Record Type",
    "Date Received",
    "Review Status",
    "Reviewed Date",
    "Compliance Status",
    "COG",
    "CYCLE",
)
JURISDICTION_FIELD_TYPES = {"name": "singleLineText", "unincorporated": "checkbox"}
RHNA_FIELD_TYPES = {
    "jurisdiction_link": "multipleRecordLinks",
    "cycle": "multipleLookupValues",
    "current": "formula",
    "he_compliance": "singleSelect",
    "review_date": "date",
}
WRITABLE_RHNA_FIELDS = ("he_compliance", "review_date")
CONFIG_FIELDS = {
    "config_version",
    "base_id",
    "jurisdictions_table_id",
    "rhna_table_id",
    "excluded_jurisdictions",
    "expected_jurisdiction_count",
    "max_out_of_compliance_share",
    "jurisdiction_fields",
    "jurisdiction_field_options_sha256",
    "rhna_fields",
    "rhna_field_options_sha256",
    "source",
    "compliance_values",
}
_SOURCE_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})(?:[ T][0-9:.]+)?\Z")
_CYCLE_RE = re.compile(r"([5-7]th) Cycle\Z")
MIN_SOURCE_ROWS = 500
MAX_SOURCE_ROWS = 700


def _fold(value: object) -> str:
    """Uppercase, accent-free, whitespace-collapsed key ("San José" == "SAN JOSE")."""

    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    return " ".join(text.upper().split())


def airtable_key(name: str) -> str:
    return _fold(_UNINCORPORATED_SUFFIX_RE.sub("", name))


def normalize_config(config: Mapping[str, object]) -> dict[str, object]:
    if set(config) != CONFIG_FIELDS:
        raise AirtableSyncConfigurationError(
            "HE compliance config keys differ from the pinned set"
        )
    if config["config_version"] != 1:
        raise AirtableSyncConfigurationError("Unsupported HE compliance config")
    for group, expected in (
        ("jurisdiction_fields", JURISDICTION_FIELD_TYPES),
        ("rhna_fields", RHNA_FIELD_TYPES),
    ):
        fields = config[group]
        digests = config[f"{group[:-1]}_options_sha256"]
        if not isinstance(fields, Mapping) or set(fields) != set(expected):
            raise AirtableSyncConfigurationError(
                f"{group} must name {sorted(expected)}"
            )
        if not isinstance(digests, Mapping) or set(digests) != set(expected):
            raise AirtableSyncConfigurationError(f"{group} option digests incomplete")
    values = config["compliance_values"]
    if not isinstance(values, Mapping) or not values:
        raise AirtableSyncConfigurationError("compliance_values must be an object")
    share = config["max_out_of_compliance_share"]
    if not isinstance(share, (int, float)) or not 0 < share < 1:
        raise AirtableSyncConfigurationError(
            "max_out_of_compliance_share must be between zero and one"
        )
    source = config["source"]
    if not isinstance(source, Mapping) or {"package_id", "resource_id"} - set(source):
        raise AirtableSyncConfigurationError("source must pin package and resource")
    return dict(config)


# --------------------------------------------------------------------- source


def parse_compliance_csv(
    data: bytes, *, compliance_values: Mapping[str, object]
) -> dict[str, dict[str, str | None]]:
    """Validate the HCD CSV and return one row per folded jurisdiction key."""

    text = data.decode("utf-8-sig")
    reader = csv.reader(io.StringIO(text, newline=""))
    header = tuple(next(reader, ()))
    if header != SOURCE_HEADER:
        raise AirtableSyncSnapshotError(
            "HCD compliance CSV header changed: " + ", ".join(header)
        )
    rows: dict[str, dict[str, str | None]] = {}
    for line_number, raw in enumerate(reader, start=2):
        if not raw:
            continue
        if len(raw) != len(SOURCE_HEADER):
            raise AirtableSyncSnapshotError(f"HCD CSV line {line_number} is malformed")
        row = dict(zip(SOURCE_HEADER, (cell.strip() for cell in raw), strict=True))
        key = _fold(row["Jurisdiction"])
        if not key:
            raise AirtableSyncSnapshotError(f"HCD CSV line {line_number} has no name")
        if key in rows:
            raise AirtableSyncSnapshotError(f"HCD CSV repeats jurisdiction {key}")
        status = row["Compliance Status"]
        if status and status not in compliance_values:
            raise AirtableSyncSnapshotError(
                f"HCD CSV has unknown compliance status {status!r} for {key}"
            )
        cycle_match = _CYCLE_RE.fullmatch(row["CYCLE"])
        if not cycle_match:
            raise AirtableSyncSnapshotError(f"HCD CSV has unknown cycle for {key}")
        reviewed = None
        if row["Reviewed Date"]:
            date_match = _SOURCE_DATE_RE.fullmatch(row["Reviewed Date"])
            if not date_match:
                raise AirtableSyncSnapshotError(f"HCD CSV has a bad date for {key}")
            reviewed = dt.date.fromisoformat(date_match.group(1)).isoformat()
        rows[key] = {
            "jurisdiction": row["Jurisdiction"],
            "cycle": cycle_match.group(1),
            "compliance_status": status,
            "review_status": row["Review Status"],
            "record_type": row["Record Type"],
            "reviewed_date": reviewed,
        }
    if not MIN_SOURCE_ROWS <= len(rows) <= MAX_SOURCE_ROWS:
        raise AirtableSyncSnapshotError(
            f"HCD CSV has {len(rows)} jurisdictions, outside the expected range"
        )
    return rows


def _http_get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "cfhe-data"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def fetch_compliance_source(
    source_config: Mapping[str, object],
    *,
    get: Callable[[str], bytes] = _http_get,
) -> tuple[bytes, dict[str, object]]:
    """Resolve the pinned CKAN resource and download its complete CSV."""

    package = json.loads(
        get(CKAN_PACKAGE_URL.format(package_id=source_config["package_id"]))
    )
    if package.get("success") is not True:
        raise AirtableSyncSnapshotError("CKAN package lookup failed")
    matches = [
        resource
        for resource in package["result"].get("resources", [])
        if resource.get("id") == source_config["resource_id"]
    ]
    if len(matches) != 1:
        raise AirtableSyncSnapshotError("Pinned HCD compliance resource is missing")
    resource = matches[0]
    url = str(resource.get("url") or "")
    if not url.startswith("https://"):
        raise AirtableSyncSnapshotError("HCD compliance resource URL is not HTTPS")
    data = get(url)
    return data, {
        "package_id": source_config["package_id"],
        "resource_id": source_config["resource_id"],
        "url": url,
        "last_modified": resource.get("last_modified"),
        "sha256": hashlib.sha256(data).hexdigest(),
        "byte_count": len(data),
    }


# ------------------------------------------------------------------- snapshot


def _clients(token: str, config: Mapping[str, object]) -> tuple[AirtableClient, ...]:
    jurisdiction_fields = config["jurisdiction_fields"]
    rhna_fields = config["rhna_fields"]
    assert isinstance(jurisdiction_fields, Mapping)
    assert isinstance(rhna_fields, Mapping)
    return (
        AirtableClient(
            token=token,
            base_id=str(config["base_id"]),
            table_id=str(config["jurisdictions_table_id"]),
            managed_field_ids=list(jurisdiction_fields.values()),
        ),
        AirtableClient(
            token=token,
            base_id=str(config["base_id"]),
            table_id=str(config["rhna_table_id"]),
            managed_field_ids=list(rhna_fields.values()),
            writable_field_ids=[rhna_fields[name] for name in WRITABLE_RHNA_FIELDS],
        ),
    )


def fetch_snapshot(
    *, token: str, config: Mapping[str, object]
) -> tuple[dict[str, object], AirtableClient]:
    config = normalize_config(config)
    jurisdiction_client, rhna_client = _clients(token, config)
    snapshot = project_snapshot(
        config=config,
        jurisdiction_schema=jurisdiction_client.get_table_schema(),
        rhna_schema=rhna_client.get_table_schema(),
        jurisdiction_export=jurisdiction_client.list_records(verify_schema=False),
        rhna_export=rhna_client.list_records(verify_schema=False),
    )
    return snapshot, rhna_client


def project_snapshot(
    *,
    config: Mapping[str, object],
    jurisdiction_schema: Mapping[str, object],
    rhna_schema: Mapping[str, object],
    jurisdiction_export: Mapping[str, object],
    rhna_export: Mapping[str, object],
) -> dict[str, object]:
    """Verify pinned field types and options, then project both tables."""

    config = normalize_config(config)
    jf = config["jurisdiction_fields"]
    rf = config["rhna_fields"]
    assert isinstance(jf, Mapping) and isinstance(rf, Mapping)
    _verify_schema(
        jurisdiction_schema,
        table_id=str(config["jurisdictions_table_id"]),
        field_ids=jf,
        expected_types=JURISDICTION_FIELD_TYPES,
        expected_options_sha256=config["jurisdiction_field_options_sha256"],
    )
    _verify_schema(
        rhna_schema,
        table_id=str(config["rhna_table_id"]),
        field_ids=rf,
        expected_types=RHNA_FIELD_TYPES,
        expected_options_sha256=config["rhna_field_options_sha256"],
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
    rhna = []
    for record in _complete_export(
        rhna_export,
        base_id=str(config["base_id"]),
        table_id=str(config["rhna_table_id"]),
    ):
        fields = record["fields"]
        label = f"RHNA record {record['record_id']}"
        links = fields.get(rf["jurisdiction_link"], [])
        compliance = fields.get(rf["he_compliance"])
        if compliance is not None and not isinstance(compliance, str):
            raise AirtableSyncSnapshotError(f"{label} compliance is not a select")
        rhna.append(
            {
                "record_id": record["record_id"],
                "jurisdiction_ids": _links(links, f"{label} jurisdiction"),
                "cycle": (
                    _cycle(fields[rf["cycle"]], f"{label} cycle")
                    if fields.get(rf["cycle"])
                    else None
                ),
                "current": _formula_boolean(
                    fields.get(rf["current"], 0), f"{label} current"
                ),
                "he_compliance": compliance,
                "review_date": _optional_date(
                    fields.get(rf["review_date"]), f"{label} review date"
                ),
            }
        )
    return {
        "complete": True,
        "base_id": config["base_id"],
        "jurisdictions": jurisdictions,
        "rhna": rhna,
    }


# ----------------------------------------------------------------------- plan


def build_plan(
    *,
    source_rows: Mapping[str, Mapping[str, str | None]],
    source_manifest: Mapping[str, object],
    snapshot: Mapping[str, object],
    config: Mapping[str, object],
) -> dict[str, object]:
    """Map every Airtable jurisdiction to HCD and list exact field changes.

    Coverage failures and implausible sources block the whole plan. A record
    that cannot be targeted safely (no single current RHNA row, a cycle that
    disagrees with HCD, or a blank HCD status) is held and left unchanged.
    """

    config = normalize_config(config)
    values = config["compliance_values"]
    assert isinstance(values, Mapping)
    excluded = {_fold(name) for name in config["excluded_jurisdictions"]}
    current_by_jurisdiction: dict[str, list[Mapping[str, object]]] = {}
    for row in snapshot["rhna"]:
        if row["current"]:
            for jurisdiction_id in row["jurisdiction_ids"]:
                current_by_jurisdiction.setdefault(jurisdiction_id, []).append(row)

    blockers: list[str] = []
    holds: list[dict[str, object]] = []
    changes: list[dict[str, object]] = []
    matched_keys: set[str] = set()
    desired_counts: Counter[str] = Counter()
    for jurisdiction in sorted(snapshot["jurisdictions"], key=lambda j: j["name"]):
        key = airtable_key(jurisdiction["name"])
        if key in excluded:
            continue
        if key in matched_keys:
            blockers.append(f"Airtable repeats jurisdiction {key}")
            continue
        source = source_rows.get(key)
        if source is None:
            blockers.append(f"{jurisdiction['name']} is not in the HCD report")
            continue
        matched_keys.add(key)
        if jurisdiction["unincorporated"] != key.endswith(" COUNTY"):
            blockers.append(f"{jurisdiction['name']} unincorporated flag disagrees")
            continue
        targets = current_by_jurisdiction.get(jurisdiction["record_id"], [])
        if len(targets) != 1:
            holds.append(
                {
                    "jurisdiction": jurisdiction["name"],
                    "reason": f"{len(targets)} current RHNA & HE records",
                }
            )
            continue
        target = targets[0]
        status = source["compliance_status"]
        if not status:
            holds.append(
                {"jurisdiction": jurisdiction["name"], "reason": "HCD status blank"}
            )
            continue
        if target["cycle"] != source["cycle"]:
            holds.append(
                {
                    "jurisdiction": jurisdiction["name"],
                    "reason": (
                        f"Airtable current cycle {target['cycle']} but HCD "
                        f"reports {source['cycle']}"
                    ),
                }
            )
            continue
        # Review Date feeds "Applied until" in the Builder's Remedy formula.
        # HCD's Reviewed Date is the latest review, which can postdate the
        # first compliance finding, so it replaces the Airtable date only when
        # the compliance status itself changes.
        compliance = values[status]
        desired = {
            "he_compliance": compliance,
            "review_date": (
                source["reviewed_date"] or target["review_date"]
                if compliance != target["he_compliance"]
                else target["review_date"]
            ),
        }
        desired_counts[str(desired["he_compliance"])] += 1
        current = {name: target[name] for name in WRITABLE_RHNA_FIELDS}
        if desired != current:
            changes.append(
                {
                    "jurisdiction": jurisdiction["name"],
                    "record_id": target["record_id"],
                    "hcd_status": status,
                    "before": current,
                    "after": desired,
                }
            )

    unmatched = sorted(set(source_rows) - matched_keys - excluded)
    blockers.extend(f"HCD jurisdiction {key} is not in Airtable" for key in unmatched)
    if len(matched_keys) != config["expected_jurisdiction_count"]:
        blockers.append(
            f"Matched {len(matched_keys)} jurisdictions, expected "
            f"{config['expected_jurisdiction_count']}"
        )
    out_share = desired_counts["Out of Compliance"] / max(
        1, sum(desired_counts.values())
    )
    if out_share > float(config["max_out_of_compliance_share"]):
        blockers.append(
            f"HCD reports {out_share:.0%} out of compliance, above the "
            "plausibility limit"
        )
    plan = {
        "plan_version": 1,
        "source": dict(source_manifest),
        "matched_count": len(matched_keys),
        "desired_counts": dict(sorted(desired_counts.items())),
        "change_count": len(changes),
        "compliance_change_count": sum(
            1
            for change in changes
            if change["before"]["he_compliance"] != change["after"]["he_compliance"]
        ),
        "hold_count": len(holds),
        "blockers": blockers,
        "apply_eligible": not blockers,
        "holds": holds,
        "changes": changes,
    }
    plan["plan_sha256"] = canonical_sha256(
        {"source": plan["source"], "changes": changes}
    )
    return plan


def plan_record_updates(
    plan: Mapping[str, object], config: Mapping[str, object]
) -> list[RecordUpdate]:
    rf = config["rhna_fields"]
    assert isinstance(rf, Mapping)
    return [
        RecordUpdate(
            record_id=str(change["record_id"]),
            expected_fields={
                rf[name]: change["before"][name] for name in WRITABLE_RHNA_FIELDS
            },
            desired_fields={
                rf[name]: change["after"][name] for name in WRITABLE_RHNA_FIELDS
            },
        )
        for change in plan["changes"]
    ]


def verify_plan_unchanged(
    plan: Mapping[str, object], fresh_plan: Mapping[str, object]
) -> None:
    if fresh_plan["plan_sha256"] != plan["plan_sha256"]:
        raise AirtableSyncStateChangedError(
            "Airtable or HCD changed between planning and publication"
        )


def require_eligible(plan: Mapping[str, object]) -> None:
    if plan["apply_eligible"] is not True:
        raise AirtableSyncBlockedError(
            "HE compliance publication is blocked: " + "; ".join(plan["blockers"])
        )


def require_complete(plan: Mapping[str, object]) -> None:
    if plan["change_count"] != 0:
        raise AirtableSyncCoverageError(
            "Airtable readback does not match the HCD compliance report"
        )


def summarize_plan(plan: Mapping[str, object]) -> dict[str, object]:
    keys = (
        "matched_count",
        "desired_counts",
        "change_count",
        "compliance_change_count",
        "hold_count",
        "apply_eligible",
        "blockers",
        "holds",
        "plan_sha256",
    )
    summary = {key: plan[key] for key in keys}
    summary["source"] = {
        key: plan["source"].get(key) for key in ("last_modified", "sha256")
    }
    summary["changes"] = [
        {
            "jurisdiction": change["jurisdiction"],
            "compliance": [
                change["before"]["he_compliance"],
                change["after"]["he_compliance"],
            ],
            "review_date": [
                change["before"]["review_date"],
                change["after"]["review_date"],
            ],
        }
        for change in plan["changes"]
    ]
    return summary


def config_option_digests(
    schema: Mapping[str, object], field_ids: Mapping[str, str]
) -> dict[str, str]:
    """Return pinned option digests for a fetched schema (setup helper)."""

    by_id = {field["id"]: field for field in schema["fields"]}
    return {
        name: canonical_sha256(by_id[field_id].get("options"))
        for name, field_id in field_ids.items()
    }


__all__: Sequence[str] = (
    "build_plan",
    "fetch_compliance_source",
    "fetch_snapshot",
    "parse_compliance_csv",
    "plan_record_updates",
    "project_snapshot",
    "summarize_plan",
)
