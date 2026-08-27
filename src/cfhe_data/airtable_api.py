"""Narrow, defensive Airtable Web API access for the publication pipeline.

The client deliberately exposes reads and updates only. Every update is bound to
an exact record ID, exact field IDs, and caller supplied expected values. It
checks fresh state before each batch and verifies state again after Airtable
acknowledges the write.
"""

from __future__ import annotations

import copy
import datetime as dt
import email.utils
import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

AIRTABLE_API_ROOT = "https://api.airtable.com/v0"
MAX_AIRTABLE_BATCH_SIZE = 10
MAX_AIRTABLE_PAGE_SIZE = 100
TRANSIENT_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504})

_BASE_ID_RE = re.compile(r"app[A-Za-z0-9]+\Z")
_TABLE_ID_RE = re.compile(r"tbl[A-Za-z0-9]+\Z")
_RECORD_ID_RE = re.compile(r"rec[A-Za-z0-9]+\Z")
_FIELD_ID_RE = re.compile(r"fld[A-Za-z0-9]+\Z")
_SAFE_ERROR_TYPE_RE = re.compile(r"[A-Za-z0-9_]+\Z")


class AirtableAPIError(RuntimeError):
    """Base class for safe Airtable API failures."""


class AirtableConfigurationError(AirtableAPIError, ValueError):
    """Client configuration is unsafe or incomplete."""


class AirtableProtocolError(AirtableAPIError):
    """Airtable returned an incomplete or malformed response."""


class AirtableSchemaError(AirtableAPIError):
    """The configured table or field IDs do not match Airtable's schema."""


class AirtableHTTPError(AirtableAPIError):
    """Airtable returned a final HTTP error response."""

    def __init__(
        self,
        *,
        status: int,
        method: str,
        endpoint: str,
        error_type: str | None = None,
    ) -> None:
        detail = f" ({error_type})" if error_type else ""
        super().__init__(
            f"Airtable returned HTTP {status}{detail} for {method} {endpoint}"
        )
        self.status = status
        self.method = method
        self.endpoint = endpoint
        self.error_type = error_type


class AirtableTransportError(AirtableAPIError):
    """A safe read could not reach Airtable after retries."""


class AirtableAmbiguousWriteError(AirtableAPIError):
    """The connection failed after a write began, so its outcome is unknown."""

    def __init__(self, record_ids: Sequence[str], *, cause_kind: str) -> None:
        ids = tuple(record_ids)
        super().__init__(
            "Airtable write outcome is unknown for record IDs: "
            + ", ".join(ids)
            + "; rerun safely so fresh state can determine what remains"
        )
        self.record_ids = ids
        self.cause_kind = cause_kind


class AirtablePreconditionError(AirtableAPIError):
    """Fresh Airtable state matched neither the reviewed nor desired values."""

    def __init__(self, conflicts: Mapping[str, Sequence[str]]) -> None:
        normalized = {
            record_id: tuple(sorted(field_ids))
            for record_id, field_ids in sorted(conflicts.items())
        }
        summary = "; ".join(
            f"{record_id}: {', '.join(field_ids)}"
            for record_id, field_ids in normalized.items()
        )
        super().__init__(
            "Airtable changed after planning; no write was sent for this batch: "
            + summary
        )
        self.conflicts = normalized


class AirtableWriteVerificationError(AirtableAPIError):
    """A write was sent but the desired values could not be verified."""

    def __init__(
        self,
        record_ids: Sequence[str],
        *,
        reason: str,
    ) -> None:
        ids = tuple(record_ids)
        super().__init__(
            "Airtable write was not verified for record IDs: "
            + ", ".join(ids)
            + f"; {reason}; rerun safely before taking further action"
        )
        self.record_ids = ids
        self.reason = reason


class TransportFailure(OSError):
    """A transport did not receive a usable HTTP response."""


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """Transport independent HTTP response."""

    status: int
    headers: Mapping[str, str]
    body: bytes


class HttpTransport(Protocol):
    """Minimal injectable HTTP transport."""

    def request(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> HttpResponse: ...


class UrllibTransport:
    """Default standard library transport."""

    def request(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> HttpResponse:
        request = urllib.request.Request(
            url=url,
            data=body,
            headers=dict(headers),
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return HttpResponse(
                    status=int(response.status),
                    headers=dict(response.headers.items()),
                    body=response.read(),
                )
        except urllib.error.HTTPError as error:
            return HttpResponse(
                status=int(error.code),
                headers=dict(error.headers.items()) if error.headers else {},
                body=error.read(),
            )
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise TransportFailure("Airtable transport failed") from error


@dataclass(frozen=True, slots=True, repr=False)
class RecordUpdate:
    """One conditional update to an existing Airtable record."""

    record_id: str
    expected_fields: Mapping[str, Any]
    desired_fields: Mapping[str, Any]

    def __repr__(self) -> str:
        field_ids = sorted(
            str(field_id)
            for field_id in (
                set(self.expected_fields.keys()) | set(self.desired_fields.keys())
            )
        )
        return (
            f"RecordUpdate(record_id={self.record_id!r}, "
            f"field_ids={field_ids!r}, values=<redacted>)"
        )


@dataclass(frozen=True, slots=True)
class _NormalizedUpdate:
    record_id: str
    expected_fields: dict[str, Any]
    desired_fields: dict[str, Any]


def _validate_id(value: object, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise AirtableConfigurationError(f"{label} is not a valid Airtable ID")
    return value


def _copy_json_value(value: Any, label: str) -> Any:
    """Validate strict JSON data and return an isolated copy."""

    def validate(item: Any, path: str) -> None:
        if item is None or isinstance(item, (str, bool, int)):
            return
        if isinstance(item, float):
            if not math.isfinite(item):
                raise AirtableConfigurationError(f"{path} must be finite")
            return
        if isinstance(item, list):
            for index, child in enumerate(item):
                validate(child, f"{path}[{index}]")
            return
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str):
                    raise AirtableConfigurationError(
                        f"{path} contains a nonstring object key"
                    )
                validate(child, f"{path}.{key}")
            return
        raise AirtableConfigurationError(f"{path} is not a JSON value")

    validate(value, label)
    return copy.deepcopy(value)


def _json_key(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _json_equal(left: Any, right: Any) -> bool:
    return _json_key(left) == _json_key(right)


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class AirtableClient:
    """Read and conditionally update one exact Airtable table."""

    def __init__(
        self,
        *,
        token: str,
        base_id: str,
        table_id: str,
        managed_field_ids: Sequence[str],
        transport: HttpTransport | None = None,
        timeout_seconds: float = 30.0,
        max_retries: int = 4,
        retry_base_seconds: float = 0.5,
        retry_cap_seconds: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], dt.datetime] = _utc_now,
    ) -> None:
        if not isinstance(token, str) or not token.strip():
            raise AirtableConfigurationError("Airtable token must be nonempty")
        if "\r" in token or "\n" in token:
            raise AirtableConfigurationError(
                "Airtable token contains invalid characters"
            )
        self._token = token.strip()
        self.base_id = _validate_id(base_id, _BASE_ID_RE, "base_id")
        self.table_id = _validate_id(table_id, _TABLE_ID_RE, "table_id")

        if isinstance(managed_field_ids, (str, bytes)):
            raise AirtableConfigurationError(
                "managed_field_ids must be a sequence of Airtable field IDs"
            )
        fields = tuple(
            _validate_id(field_id, _FIELD_ID_RE, "managed field ID")
            for field_id in managed_field_ids
        )
        if not fields:
            raise AirtableConfigurationError("managed_field_ids cannot be empty")
        if len(set(fields)) != len(fields):
            raise AirtableConfigurationError("managed_field_ids contains duplicates")
        self.managed_field_ids = tuple(sorted(fields))
        self._managed_field_set = frozenset(fields)

        if not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
            raise AirtableConfigurationError("timeout_seconds must be positive")
        if isinstance(max_retries, bool) or not isinstance(max_retries, int):
            raise AirtableConfigurationError("max_retries must be an integer")
        if max_retries < 0 or max_retries > 10:
            raise AirtableConfigurationError("max_retries must be between 0 and 10")
        if retry_base_seconds < 0 or retry_cap_seconds < 0:
            raise AirtableConfigurationError("retry delays cannot be negative")
        if retry_base_seconds > retry_cap_seconds:
            raise AirtableConfigurationError(
                "retry_base_seconds cannot exceed retry_cap_seconds"
            )
        self._transport = transport or UrllibTransport()
        self._timeout_seconds = float(timeout_seconds)
        self._max_retries = max_retries
        self._retry_base_seconds = float(retry_base_seconds)
        self._retry_cap_seconds = float(retry_cap_seconds)
        self._sleep = sleep
        self._now = now

    def __repr__(self) -> str:
        return (
            f"AirtableClient(base_id={self.base_id!r}, "
            f"table_id={self.table_id!r}, token=<redacted>)"
        )

    def get_table_schema(self) -> dict[str, Any]:
        """Fetch the table schema and verify every managed field ID exists."""

        payload = self._request_json(
            method="GET",
            endpoint=f"meta/bases/{self.base_id}/tables",
        )
        tables = payload.get("tables")
        if not isinstance(tables, list):
            raise AirtableProtocolError("Airtable schema response has no tables array")
        matches = [
            table
            for table in tables
            if isinstance(table, Mapping) and table.get("id") == self.table_id
        ]
        if len(matches) != 1:
            raise AirtableSchemaError(
                "Configured Airtable table ID was not found exactly once"
            )
        table = matches[0]
        fields = table.get("fields")
        if not isinstance(fields, list):
            raise AirtableProtocolError("Airtable table schema has no fields array")

        projected: list[dict[str, str]] = []
        seen: set[str] = set()
        for position, raw_field in enumerate(fields):
            if not isinstance(raw_field, Mapping):
                raise AirtableProtocolError(
                    f"Airtable schema field {position} is not an object"
                )
            field_id = raw_field.get("id")
            name = raw_field.get("name")
            field_type = raw_field.get("type")
            if not isinstance(field_id, str) or not _FIELD_ID_RE.fullmatch(field_id):
                raise AirtableProtocolError(
                    f"Airtable schema field {position} has no valid field ID"
                )
            if field_id in seen:
                raise AirtableProtocolError(
                    f"Airtable schema repeats field ID {field_id}"
                )
            if not isinstance(name, str) or not isinstance(field_type, str):
                raise AirtableProtocolError(
                    f"Airtable schema field {field_id} lacks name or type"
                )
            seen.add(field_id)
            projected.append({"id": field_id, "name": name, "type": field_type})

        missing = sorted(self._managed_field_set - seen)
        if missing:
            raise AirtableSchemaError(
                "Managed Airtable field IDs are absent from the target table: "
                + ", ".join(missing)
            )
        table_name = table.get("name")
        if not isinstance(table_name, str):
            raise AirtableProtocolError("Airtable table schema has no name")
        projected.sort(key=lambda item: item["id"])
        return {
            "complete": True,
            "base_id": self.base_id,
            "table_id": self.table_id,
            "table_name": table_name,
            "field_count": len(projected),
            "managed_field_ids": list(self.managed_field_ids),
            "fields": projected,
        }

    def list_records(self, *, verify_schema: bool = True) -> dict[str, Any]:
        """Read every page and advertise completeness only at the terminal page."""

        if verify_schema:
            self.get_table_schema()
        records: list[dict[str, Any]] = []
        record_ids: set[str] = set()
        seen_offsets: set[str] = set()
        offset: str | None = None
        page_count = 0

        while True:
            query: list[tuple[str, str]] = [
                ("pageSize", str(MAX_AIRTABLE_PAGE_SIZE)),
                ("returnFieldsByFieldId", "true"),
            ]
            query.extend(("fields[]", field_id) for field_id in self.managed_field_ids)
            if offset is not None:
                query.append(("offset", offset))
            payload = self._request_json(
                method="GET",
                endpoint=f"{self.base_id}/{self.table_id}",
                query=query,
            )
            raw_records = payload.get("records")
            if not isinstance(raw_records, list):
                raise AirtableProtocolError(
                    "Airtable records page has no records array"
                )
            if len(raw_records) > MAX_AIRTABLE_PAGE_SIZE:
                raise AirtableProtocolError("Airtable records page exceeds pageSize")
            page_count += 1
            for raw_record in raw_records:
                record = self._normalize_record(raw_record)
                record_id = record["id"]
                if record_id in record_ids:
                    raise AirtableProtocolError(
                        f"Airtable pagination repeated record ID {record_id}"
                    )
                record_ids.add(record_id)
                records.append(record)

            next_offset = payload.get("offset")
            if next_offset is None:
                break
            if not isinstance(next_offset, str) or not next_offset:
                raise AirtableProtocolError("Airtable returned an invalid offset")
            if next_offset in seen_offsets:
                raise AirtableProtocolError("Airtable pagination repeated an offset")
            if not raw_records:
                raise AirtableProtocolError(
                    "Airtable returned an empty page with a continuation offset"
                )
            seen_offsets.add(next_offset)
            offset = next_offset

        return {
            "complete": True,
            "base_id": self.base_id,
            "table_id": self.table_id,
            "requested_field_ids": list(self.managed_field_ids),
            "page_count": page_count,
            "record_count": len(records),
            "terminal_offset_reached": True,
            "retrieved_at": self._now()
            .astimezone(dt.UTC)
            .isoformat()
            .replace("+00:00", "Z"),
            "records": records,
        }

    def update_records(
        self,
        updates: Sequence[RecordUpdate],
        *,
        batch_size: int = MAX_AIRTABLE_BATCH_SIZE,
        verify_schema: bool = True,
    ) -> dict[str, Any]:
        """Conditionally update existing records, then verify fresh readback."""

        if isinstance(batch_size, bool) or not isinstance(batch_size, int):
            raise AirtableConfigurationError("batch_size must be an integer")
        if not 1 <= batch_size <= MAX_AIRTABLE_BATCH_SIZE:
            raise AirtableConfigurationError("batch_size must be between 1 and 10")
        normalized = self._normalize_updates(updates)
        if not normalized:
            return self._result([], batch_count=0)
        if verify_schema:
            self.get_table_schema()

        statuses: list[dict[str, Any]] = []
        completed_batches = 0
        for start in range(0, len(normalized), batch_size):
            batch = normalized[start : start + batch_size]
            already_current: list[_NormalizedUpdate] = []
            pending: list[_NormalizedUpdate] = []
            conflicts: dict[str, list[str]] = {}

            for update in batch:
                record = self._read_record(update.record_id)
                fields = record["fields"]
                desired_matches = {
                    field_id: _json_equal(fields.get(field_id), desired)
                    for field_id, desired in update.desired_fields.items()
                }
                expected_matches = {
                    field_id: _json_equal(
                        fields.get(field_id), update.expected_fields[field_id]
                    )
                    for field_id in update.expected_fields
                }
                if all(desired_matches.values()):
                    already_current.append(update)
                elif all(expected_matches.values()):
                    pending.append(update)
                else:
                    conflicts[update.record_id] = sorted(
                        field_id
                        for field_id in update.expected_fields
                        if not expected_matches[field_id]
                        and not desired_matches[field_id]
                    )
                    if not conflicts[update.record_id]:
                        conflicts[update.record_id] = sorted(update.expected_fields)

            if conflicts:
                raise AirtablePreconditionError(conflicts)

            statuses.extend(
                self._record_status(update, "already_current")
                for update in already_current
            )
            if pending:
                self._patch_records(pending)
                self._verify_written_records(pending)
                statuses.extend(
                    self._record_status(update, "updated") for update in pending
                )
            completed_batches += 1

        order = {update.record_id: index for index, update in enumerate(normalized)}
        statuses.sort(key=lambda item: order[str(item["record_id"])])
        return self._result(statuses, batch_count=completed_batches)

    def _normalize_updates(
        self, updates: Sequence[RecordUpdate]
    ) -> list[_NormalizedUpdate]:
        if isinstance(updates, (str, bytes)) or not isinstance(updates, Sequence):
            raise AirtableConfigurationError(
                "updates must be a sequence of RecordUpdate objects"
            )
        normalized: list[_NormalizedUpdate] = []
        seen: set[str] = set()
        for position, update in enumerate(updates):
            if not isinstance(update, RecordUpdate):
                raise AirtableConfigurationError(
                    f"updates[{position}] must be a RecordUpdate"
                )
            record_id = _validate_id(
                update.record_id, _RECORD_ID_RE, f"updates[{position}].record_id"
            )
            if record_id in seen:
                raise AirtableConfigurationError(
                    f"updates contains duplicate record ID {record_id}"
                )
            seen.add(record_id)
            if not isinstance(update.expected_fields, Mapping) or not isinstance(
                update.desired_fields, Mapping
            ):
                raise AirtableConfigurationError(
                    f"updates[{position}] fields must be objects"
                )
            expected_keys = set(update.expected_fields)
            desired_keys = set(update.desired_fields)
            if not expected_keys or expected_keys != desired_keys:
                raise AirtableConfigurationError(
                    f"updates[{position}] must have the same nonempty expected and desired field IDs"
                )
            invalid = [
                field_id
                for field_id in expected_keys
                if not isinstance(field_id, str)
                or not _FIELD_ID_RE.fullmatch(field_id)
                or field_id not in self._managed_field_set
            ]
            if invalid:
                raise AirtableConfigurationError(
                    f"updates[{position}] has unmanaged or invalid field IDs"
                )
            expected = {
                field_id: _copy_json_value(
                    update.expected_fields[field_id],
                    f"updates[{position}].expected_fields.{field_id}",
                )
                for field_id in sorted(expected_keys)
            }
            desired = {
                field_id: _copy_json_value(
                    update.desired_fields[field_id],
                    f"updates[{position}].desired_fields.{field_id}",
                )
                for field_id in sorted(desired_keys)
            }
            normalized.append(
                _NormalizedUpdate(
                    record_id=record_id,
                    expected_fields=expected,
                    desired_fields=desired,
                )
            )
        return normalized

    def _read_record(self, record_id: str) -> dict[str, Any]:
        payload = self._request_json(
            method="GET",
            endpoint=f"{self.base_id}/{self.table_id}/{record_id}",
            query=[("returnFieldsByFieldId", "true")],
        )
        record = self._normalize_record(payload)
        if record["id"] != record_id:
            raise AirtableProtocolError(
                f"Airtable returned record {record['id']} when {record_id} was requested"
            )
        return record

    def _patch_records(self, updates: Sequence[_NormalizedUpdate]) -> None:
        if not 1 <= len(updates) <= MAX_AIRTABLE_BATCH_SIZE:
            raise AirtableConfigurationError(
                "Internal Airtable update batch must contain 1 through 10 records"
            )
        record_ids = [update.record_id for update in updates]
        try:
            payload = self._request_json(
                method="PATCH",
                endpoint=f"{self.base_id}/{self.table_id}",
                query=[("returnFieldsByFieldId", "true")],
                payload={
                    "typecast": False,
                    "records": [
                        {
                            "id": update.record_id,
                            "fields": update.desired_fields,
                        }
                        for update in updates
                    ],
                },
                write_record_ids=record_ids,
            )
        except AirtableAmbiguousWriteError:
            raise
        except AirtableHTTPError as error:
            if error.status in {500, 502, 503, 504}:
                raise AirtableAmbiguousWriteError(
                    record_ids, cause_kind="server_error"
                ) from error
            raise

        raw_records = payload.get("records")
        if not isinstance(raw_records, list):
            raise AirtableWriteVerificationError(
                record_ids, reason="the PATCH response omitted its records array"
            )
        response_ids: list[str] = []
        for raw_record in raw_records:
            record = self._normalize_record(raw_record)
            response_ids.append(record["id"])
        if len(response_ids) != len(set(response_ids)) or set(response_ids) != set(
            record_ids
        ):
            raise AirtableWriteVerificationError(
                record_ids,
                reason="the PATCH response did not acknowledge exactly the requested IDs",
            )

    def _verify_written_records(self, updates: Sequence[_NormalizedUpdate]) -> None:
        failures: list[str] = []
        for update in updates:
            try:
                record = self._read_record(update.record_id)
            except AirtableAPIError as error:
                raise AirtableWriteVerificationError(
                    [item.record_id for item in updates],
                    reason="fresh readback failed",
                ) from error
            fields = record["fields"]
            if not all(
                _json_equal(fields.get(field_id), desired)
                for field_id, desired in update.desired_fields.items()
            ):
                failures.append(update.record_id)
        if failures:
            raise AirtableWriteVerificationError(
                failures, reason="fresh readback did not match desired values"
            )

    def _normalize_record(self, raw_record: object) -> dict[str, Any]:
        if not isinstance(raw_record, Mapping):
            raise AirtableProtocolError("Airtable record is not an object")
        record_id = raw_record.get("id")
        if not isinstance(record_id, str) or not _RECORD_ID_RE.fullmatch(record_id):
            raise AirtableProtocolError("Airtable record has no valid record ID")
        raw_fields = raw_record.get("fields")
        if not isinstance(raw_fields, Mapping):
            raise AirtableProtocolError(
                f"Airtable record {record_id} has no fields object"
            )
        for field_id in raw_fields:
            if not isinstance(field_id, str) or not _FIELD_ID_RE.fullmatch(field_id):
                raise AirtableProtocolError(
                    "Airtable did not return field IDs as requested"
                )
        fields = {
            field_id: _copy_json_value(raw_fields[field_id], f"record.{field_id}")
            for field_id in self.managed_field_ids
            if field_id in raw_fields
        }
        return {"id": record_id, "fields": fields}

    def _request_json(
        self,
        *,
        method: str,
        endpoint: str,
        query: Sequence[tuple[str, str]] | None = None,
        payload: Mapping[str, Any] | None = None,
        write_record_ids: Sequence[str] = (),
    ) -> dict[str, Any]:
        if method not in {"GET", "PATCH"}:
            raise AirtableConfigurationError("Unsupported Airtable HTTP method")
        if payload is not None and method != "PATCH":
            raise AirtableConfigurationError("Only PATCH requests may have a body")
        url = f"{AIRTABLE_API_ROOT}/{endpoint}"
        if query:
            url += "?" + urllib.parse.urlencode(list(query), doseq=True)
        body = None
        if payload is not None:
            body = json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._token}",
            "User-Agent": "cfhe-data/0.1",
        }
        if body is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"

        for attempt in range(self._max_retries + 1):
            try:
                response = self._transport.request(
                    method=method,
                    url=url,
                    headers=headers,
                    body=body,
                    timeout=self._timeout_seconds,
                )
            except TransportFailure as error:
                if method == "PATCH":
                    raise AirtableAmbiguousWriteError(
                        write_record_ids, cause_kind="transport"
                    ) from error
                if attempt >= self._max_retries:
                    raise AirtableTransportError(
                        f"Airtable GET transport failed after {attempt + 1} attempts"
                    ) from error
                self._sleep(self._retry_delay(attempt, None))
                continue

            self._validate_http_response(response)
            if 200 <= response.status < 300:
                try:
                    parsed = json.loads(response.body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise AirtableProtocolError(
                        "Airtable returned invalid JSON"
                    ) from error
                if not isinstance(parsed, dict):
                    raise AirtableProtocolError(
                        "Airtable JSON response is not an object"
                    )
                return parsed

            if (
                response.status in TRANSIENT_HTTP_STATUSES
                and attempt < self._max_retries
            ):
                self._sleep(
                    self._retry_delay(
                        attempt, self._header(response.headers, "Retry-After")
                    )
                )
                continue
            error_type = self._safe_error_type(response.body)
            raise AirtableHTTPError(
                status=response.status,
                method=method,
                endpoint=endpoint,
                error_type=error_type,
            )

        raise AssertionError("Airtable request loop exhausted unexpectedly")

    def _retry_delay(self, attempt: int, retry_after: str | None) -> float:
        exponential = min(
            self._retry_cap_seconds,
            self._retry_base_seconds * (2**attempt),
        )
        advertised = self._parse_retry_after(retry_after)
        return min(self._retry_cap_seconds, max(exponential, advertised))

    def _parse_retry_after(self, value: str | None) -> float:
        if value is None:
            return 0.0
        try:
            seconds = float(value.strip())
            if math.isfinite(seconds):
                return max(0.0, seconds)
        except ValueError:
            pass
        try:
            when = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return 0.0
        if when.tzinfo is None:
            when = when.replace(tzinfo=dt.UTC)
        now = self._now()
        if now.tzinfo is None:
            now = now.replace(tzinfo=dt.UTC)
        return max(0.0, (when - now).total_seconds())

    @staticmethod
    def _validate_http_response(response: object) -> None:
        if not isinstance(response, HttpResponse):
            raise AirtableProtocolError(
                "Airtable transport returned an invalid response"
            )
        if (
            isinstance(response.status, bool)
            or not isinstance(response.status, int)
            or not 100 <= response.status <= 599
        ):
            raise AirtableProtocolError("Airtable transport returned an invalid status")
        if not isinstance(response.headers, Mapping):
            raise AirtableProtocolError("Airtable transport returned invalid headers")
        if not isinstance(response.body, bytes):
            raise AirtableProtocolError("Airtable transport returned a nonbytes body")

    @staticmethod
    def _header(headers: Mapping[str, str], name: str) -> str | None:
        for key, value in headers.items():
            if isinstance(key, str) and key.casefold() == name.casefold():
                return value if isinstance(value, str) else None
        return None

    @staticmethod
    def _safe_error_type(body: bytes) -> str | None:
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, Mapping):
            return None
        error = payload.get("error")
        if isinstance(error, Mapping):
            candidate = error.get("type")
        elif isinstance(error, str):
            candidate = error
        else:
            return None
        if isinstance(candidate, str) and _SAFE_ERROR_TYPE_RE.fullmatch(candidate):
            return candidate
        return None

    @staticmethod
    def _record_status(update: _NormalizedUpdate, status: str) -> dict[str, Any]:
        return {
            "record_id": update.record_id,
            "status": status,
            "field_ids": sorted(update.desired_fields),
            "values": "redacted",
        }

    def _result(
        self, statuses: Sequence[Mapping[str, Any]], *, batch_count: int
    ) -> dict[str, Any]:
        updated = sum(item.get("status") == "updated" for item in statuses)
        already = sum(item.get("status") == "already_current" for item in statuses)
        return {
            "complete": True,
            "base_id": self.base_id,
            "table_id": self.table_id,
            "requested_record_count": len(statuses),
            "updated_record_count": updated,
            "already_current_record_count": already,
            "verified_record_count": len(statuses),
            "batch_count": batch_count,
            "records": [dict(item) for item in statuses],
            "values": "redacted",
        }
