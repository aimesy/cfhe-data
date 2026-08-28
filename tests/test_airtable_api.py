from __future__ import annotations

import datetime as dt
import json
import urllib.parse
from collections.abc import Mapping
from typing import Any

import pytest

from cfhe_data.airtable_api import (
    AirtableAmbiguousWriteError,
    AirtableClient,
    AirtableConfigurationError,
    AirtableHTTPError,
    AirtablePreconditionError,
    AirtableProtocolError,
    AirtableSchemaError,
    AirtableWriteVerificationError,
    HttpResponse,
    RecordUpdate,
    TransportFailure,
)

BASE_ID = "appBase123"
TABLE_ID = "tblTable123"
FIELD_VLI = "fldVli123"
FIELD_LI = "fldLi123"
FIELD_LINK = "fldLink123"
TOKEN = "test-token-never-log"


def response(
    payload: Mapping[str, Any], status: int = 200, **headers: str
) -> HttpResponse:
    return HttpResponse(
        status=status,
        headers=headers,
        body=json.dumps(payload).encode("utf-8"),
    )


def record(record_id: str, **fields: Any) -> dict[str, Any]:
    return {"id": record_id, "fields": fields}


def schema_payload(
    field_ids: tuple[str, ...] = (FIELD_VLI, FIELD_LI),
    *,
    options_by_id: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    options_by_id = options_by_id or {}
    return {
        "tables": [
            {
                "id": TABLE_ID,
                "name": "RHNA & HE",
                "fields": [
                    {
                        "id": field_id,
                        "name": field_id,
                        "type": "number",
                        **(
                            {"options": options_by_id[field_id]}
                            if field_id in options_by_id
                            else {}
                        ),
                    }
                    for field_id in field_ids
                ],
            }
        ]
    }


class QueueTransport:
    def __init__(self, outcomes: list[HttpResponse | BaseException]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []

    def request(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> HttpResponse:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "body": body,
                "timeout": timeout,
            }
        )
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class StatefulTransport:
    def __init__(self, records: Mapping[str, Mapping[str, Any]]) -> None:
        self.records = {
            record_id: dict(fields) for record_id, fields in records.items()
        }
        self.calls: list[dict[str, Any]] = []
        self.patch_sizes: list[int] = []
        self.patch_count = 0
        self.ambiguous_after_apply = False
        self.mismatch_after_patch = False
        self.stale_reads_after_patch = 0
        self._stale_records: dict[str, dict[str, Any]] = {}
        self.transient_patch_statuses: list[int] = []
        self.mutate_after_next_read: tuple[str, str, Any] | None = None

    def request(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> HttpResponse:
        call = {
            "method": method,
            "url": url,
            "headers": dict(headers),
            "body": body,
            "timeout": timeout,
        }
        self.calls.append(call)
        parsed = urllib.parse.urlparse(url)
        path = parsed.path
        if method == "GET" and path.endswith(f"/meta/bases/{BASE_ID}/tables"):
            return response(schema_payload())
        if method == "GET":
            record_id = path.rsplit("/", 1)[-1]
            source = self.records[record_id]
            if self.stale_reads_after_patch > 0 and record_id in self._stale_records:
                source = self._stale_records[record_id]
                self.stale_reads_after_patch -= 1
            result = response(record(record_id, **source))
            if self.mutate_after_next_read is not None:
                target_id, field_id, value = self.mutate_after_next_read
                self.records[target_id][field_id] = value
                self.mutate_after_next_read = None
            return result
        if method != "PATCH" or body is None:
            raise AssertionError(f"Unexpected request: {method} {url}")

        self.patch_count += 1
        if self.transient_patch_statuses:
            status = self.transient_patch_statuses.pop(0)
            return response({"error": {"type": "SERVER_ERROR"}}, status=status)
        payload = json.loads(body)
        assert "performUpsert" not in payload
        assert payload["typecast"] is False
        updates = payload["records"]
        self.patch_sizes.append(len(updates))
        for item in updates:
            assert set(item) == {"id", "fields"}
            assert item["id"] in self.records
            self._stale_records[item["id"]] = dict(self.records[item["id"]])
            self.records[item["id"]].update(item["fields"])
        if self.ambiguous_after_apply:
            self.ambiguous_after_apply = False
            raise TransportFailure("socket closed")
        if self.mismatch_after_patch:
            first = updates[0]
            first_field = next(iter(first["fields"]))
            self.records[first["id"]][first_field] = -999
        return response(
            {
                "records": [
                    record(item["id"], **self.records[item["id"]]) for item in updates
                ]
            }
        )


def client(
    transport: Any,
    *,
    fields: tuple[str, ...] = (FIELD_VLI, FIELD_LI),
    writable_fields: tuple[str, ...] | None = None,
    sleeps: list[float] | None = None,
    max_retries: int = 2,
) -> AirtableClient:
    delays = sleeps if sleeps is not None else []
    return AirtableClient(
        token=TOKEN,
        base_id=BASE_ID,
        table_id=TABLE_ID,
        managed_field_ids=fields,
        writable_field_ids=fields if writable_fields is None else writable_fields,
        transport=transport,
        max_retries=max_retries,
        retry_base_seconds=0.5,
        retry_cap_seconds=30,
        sleep=delays.append,
        now=lambda: dt.datetime(2026, 8, 26, 12, tzinfo=dt.UTC),
    )


def update(
    record_id: str,
    before: tuple[int, int],
    after: tuple[int, int],
) -> RecordUpdate:
    return RecordUpdate(
        record_id=record_id,
        expected_fields={FIELD_VLI: before[0], FIELD_LI: before[1]},
        desired_fields={FIELD_VLI: after[0], FIELD_LI: after[1]},
    )


def test_schema_get_verifies_exact_managed_field_ids() -> None:
    options = {"precision": 0}
    transport = QueueTransport(
        [response(schema_payload(options_by_id={FIELD_VLI: options}))]
    )

    result = client(transport).get_table_schema()

    assert result["complete"] is True
    assert result["managed_field_ids"] == [FIELD_LI, FIELD_VLI]
    assert result["writable_field_ids"] == [FIELD_LI, FIELD_VLI]
    assert result["field_count"] == 2
    fields = {item["id"]: item for item in result["fields"]}
    assert fields[FIELD_VLI]["options"] == options
    assert fields[FIELD_LI]["options"] is None
    call = transport.calls[0]
    assert call["method"] == "GET"
    assert call["url"].endswith(f"/meta/bases/{BASE_ID}/tables")
    assert call["headers"]["Authorization"] == f"Bearer {TOKEN}"


def test_schema_get_fails_when_a_managed_field_id_is_absent() -> None:
    transport = QueueTransport([response(schema_payload((FIELD_VLI,)))])

    with pytest.raises(AirtableSchemaError, match=FIELD_LI):
        client(transport).get_table_schema()


def test_list_records_reads_to_terminal_offset_and_advertises_completeness() -> None:
    transport = QueueTransport(
        [
            response(schema_payload()),
            response(
                {
                    "records": [record("recOne123", **{FIELD_VLI: 1})],
                    "offset": "itrNext123",
                }
            ),
            response({"records": [record("recTwo123", **{FIELD_VLI: 2, FIELD_LI: 3})]}),
        ]
    )

    result = client(transport).list_records()

    assert result["complete"] is True
    assert result["terminal_offset_reached"] is True
    assert result["page_count"] == 2
    assert result["record_count"] == 2
    assert result["retrieved_at"] == "2026-08-26T12:00:00Z"
    first_query = urllib.parse.parse_qs(
        urllib.parse.urlparse(transport.calls[1]["url"]).query
    )
    assert first_query["pageSize"] == ["100"]
    assert first_query["returnFieldsByFieldId"] == ["true"]
    assert set(first_query["fields[]"]) == {FIELD_VLI, FIELD_LI}
    second_query = urllib.parse.parse_qs(
        urllib.parse.urlparse(transport.calls[2]["url"]).query
    )
    assert second_query["offset"] == ["itrNext123"]


def test_list_records_rejects_repeated_records_across_pages() -> None:
    transport = QueueTransport(
        [
            response({"records": [record("recOne123")], "offset": "itrNext123"}),
            response({"records": [record("recOne123")]}),
        ]
    )

    with pytest.raises(AirtableProtocolError, match="repeated record ID"):
        client(transport).list_records(verify_schema=False)


def test_update_checks_schema_prevalues_and_readback() -> None:
    transport = StatefulTransport({"recOne123": {FIELD_VLI: 1, FIELD_LI: 2}})

    result = client(transport).update_records([update("recOne123", (1, 2), (10, 20))])

    assert transport.records["recOne123"] == {FIELD_VLI: 10, FIELD_LI: 20}
    assert [call["method"] for call in transport.calls] == [
        "GET",
        "GET",
        "GET",
        "PATCH",
        "GET",
    ]
    patch = json.loads(transport.calls[3]["body"])
    patch_query = urllib.parse.parse_qs(
        urllib.parse.urlparse(transport.calls[3]["url"]).query
    )
    assert patch_query == {}
    assert patch == {
        "returnFieldsByFieldId": True,
        "typecast": False,
        "records": [
            {
                "id": "recOne123",
                "fields": {FIELD_LI: 20, FIELD_VLI: 10},
            }
        ],
    }
    assert result["complete"] is True
    assert result["updated_record_count"] == 1
    assert result["verified_record_count"] == 1
    assert result["records"] == [
        {
            "record_id": "recOne123",
            "status": "updated",
            "field_ids": [FIELD_LI, FIELD_VLI],
            "values": "redacted",
        }
    ]
    assert "10" not in json.dumps(result)
    assert TOKEN not in json.dumps(result)


def test_already_current_record_is_verified_without_patch() -> None:
    transport = StatefulTransport({"recOne123": {FIELD_VLI: 10, FIELD_LI: 20}})

    result = client(transport).update_records(
        [update("recOne123", (1, 2), (10, 20))], verify_schema=False
    )

    assert transport.patch_count == 0
    assert result["updated_record_count"] == 0
    assert result["already_current_record_count"] == 1
    assert result["verified_record_count"] == 1


def test_changed_prevalue_fails_before_a_patch() -> None:
    transport = StatefulTransport({"recOne123": {FIELD_VLI: 999, FIELD_LI: 2}})

    with pytest.raises(AirtablePreconditionError) as caught:
        client(transport).update_records(
            [update("recOne123", (1, 2), (10, 20))], verify_schema=False
        )

    assert transport.patch_count == 0
    assert caught.value.conflicts == {"recOne123": (FIELD_VLI,)}
    assert "999" not in str(caught.value)


def test_second_prepatch_read_catches_an_interactive_edit() -> None:
    transport = StatefulTransport({"recOne123": {FIELD_VLI: 1, FIELD_LI: 2}})
    transport.mutate_after_next_read = ("recOne123", FIELD_VLI, 999)

    with pytest.raises(AirtablePreconditionError) as caught:
        client(transport).update_records(
            [update("recOne123", (1, 2), (10, 20))], verify_schema=False
        )

    assert transport.patch_count == 0
    assert caught.value.conflicts == {"recOne123": (FIELD_VLI,)}


def test_mixed_old_and_new_fields_fail_closed() -> None:
    transport = StatefulTransport({"recOne123": {FIELD_VLI: 10, FIELD_LI: 2}})

    with pytest.raises(AirtablePreconditionError):
        client(transport).update_records(
            [update("recOne123", (1, 2), (10, 20))], verify_schema=False
        )

    assert transport.patch_count == 0


def test_updates_are_split_into_batches_no_larger_than_ten() -> None:
    state = {
        f"recItem{index}": {FIELD_VLI: index, FIELD_LI: index} for index in range(11)
    }
    updates = [
        update(f"recItem{index}", (index, index), (index + 100, index + 200))
        for index in range(11)
    ]
    transport = StatefulTransport(state)

    result = client(transport).update_records(updates, verify_schema=False)

    assert transport.patch_sizes == [10, 1]
    assert result["batch_count"] == 2
    assert result["updated_record_count"] == 11


def test_429_honors_retry_after_then_succeeds() -> None:
    sleeps: list[float] = []
    transport = QueueTransport(
        [
            response(
                {"error": {"type": "RATE_LIMITED"}},
                status=429,
                **{"Retry-After": "7"},
            ),
            response(schema_payload()),
        ]
    )

    client(transport, sleeps=sleeps).get_table_schema()

    assert sleeps == [7.0]
    assert len(transport.calls) == 2


def test_transient_patch_response_retries_idempotently() -> None:
    sleeps: list[float] = []
    transport = StatefulTransport({"recOne123": {FIELD_VLI: 1, FIELD_LI: 2}})
    transport.transient_patch_statuses = [503]

    result = client(transport, sleeps=sleeps).update_records(
        [update("recOne123", (1, 2), (10, 20))], verify_schema=False
    )

    assert sleeps == [0.5]
    assert transport.patch_count == 2
    assert result["updated_record_count"] == 1


def test_transport_failure_during_patch_is_ambiguous_and_not_retried() -> None:
    transport = StatefulTransport({"recOne123": {FIELD_VLI: 1, FIELD_LI: 2}})
    transport.ambiguous_after_apply = True

    with pytest.raises(AirtableAmbiguousWriteError) as caught:
        client(transport).update_records(
            [update("recOne123", (1, 2), (10, 20))], verify_schema=False
        )

    assert caught.value.cause_kind == "transport"
    assert caught.value.record_ids == ("recOne123",)
    assert transport.patch_count == 1


def test_rerun_after_ambiguous_patch_safely_resumes_without_second_patch() -> None:
    transport = StatefulTransport({"recOne123": {FIELD_VLI: 1, FIELD_LI: 2}})
    transport.ambiguous_after_apply = True
    api = client(transport)
    change = update("recOne123", (1, 2), (10, 20))
    with pytest.raises(AirtableAmbiguousWriteError):
        api.update_records([change], verify_schema=False)

    result = api.update_records([change], verify_schema=False)

    assert transport.patch_count == 1
    assert result["already_current_record_count"] == 1
    assert result["verified_record_count"] == 1


def test_failed_postwrite_readback_stops_further_work() -> None:
    transport = StatefulTransport({"recOne123": {FIELD_VLI: 1, FIELD_LI: 2}})
    transport.mismatch_after_patch = True

    with pytest.raises(AirtableWriteVerificationError, match="did not match"):
        client(transport).update_records(
            [update("recOne123", (1, 2), (10, 20))], verify_schema=False
        )


def test_postwrite_readback_retries_airtable_visibility_lag() -> None:
    sleeps: list[float] = []
    transport = StatefulTransport({"recOne123": {FIELD_VLI: 1, FIELD_LI: 2}})
    transport.stale_reads_after_patch = 1

    result = client(transport, sleeps=sleeps).update_records(
        [update("recOne123", (1, 2), (10, 20))], verify_schema=False
    )

    assert sleeps == [0.5]
    assert result["updated_record_count"] == 1


def test_deferred_readback_waits_once_after_all_batches() -> None:
    sleeps: list[float] = []
    state = {
        f"recItem{index}": {FIELD_VLI: index, FIELD_LI: index} for index in range(11)
    }
    updates = [
        update(f"recItem{index}", (index, index), (index + 100, index + 200))
        for index in range(11)
    ]
    transport = StatefulTransport(state)

    result = client(transport, sleeps=sleeps).update_records(
        updates, verify_schema=False, deferred_readback_seconds=10
    )

    assert transport.patch_sizes == [10, 1]
    assert sleeps == [10.0]
    assert result["verified_record_count"] == 11


def test_token_and_airtable_error_message_are_redacted() -> None:
    unsafe_message = f"invalid value containing {TOKEN}"
    transport = QueueTransport(
        [
            response(
                {
                    "error": {
                        "type": "INVALID_REQUEST",
                        "message": unsafe_message,
                    }
                },
                status=422,
            )
        ]
    )
    api = client(transport)

    with pytest.raises(AirtableHTTPError) as caught:
        api.get_table_schema()

    assert "INVALID_REQUEST" in str(caught.value)
    assert unsafe_message not in str(caught.value)
    assert TOKEN not in str(caught.value)
    assert TOKEN not in repr(api)
    assert TOKEN not in repr(
        RecordUpdate(
            "recOne123",
            {FIELD_VLI: TOKEN},
            {FIELD_VLI: "different"},
        )
    )


@pytest.mark.parametrize(
    "bad_update",
    [
        RecordUpdate("recOne123", {"VLI": 1}, {"VLI": 2}),
        RecordUpdate("recOne123", {"fldOther123": 1}, {"fldOther123": 2}),
        RecordUpdate("recOne123", {FIELD_VLI: 1}, {FIELD_LI: 2}),
    ],
)
def test_updates_require_exact_managed_ids_and_matching_prevalues(
    bad_update: RecordUpdate,
) -> None:
    transport = QueueTransport([])

    with pytest.raises(AirtableConfigurationError):
        client(transport).update_records([bad_update], verify_schema=False)

    assert transport.calls == []


def test_read_fields_are_not_writable_without_explicit_allowlisting() -> None:
    transport = QueueTransport([])
    api = client(
        transport,
        fields=(FIELD_VLI, FIELD_LI, FIELD_LINK),
        writable_fields=(FIELD_VLI, FIELD_LI),
    )
    link_update = RecordUpdate(
        "recOne123",
        {FIELD_LINK: ["recOld123"]},
        {FIELD_LINK: ["recNew123"]},
    )

    with pytest.raises(AirtableConfigurationError, match="unwritable"):
        api.update_records([link_update], verify_schema=False)

    assert transport.calls == []


def test_writable_fields_must_be_managed_read_fields() -> None:
    with pytest.raises(AirtableConfigurationError, match="subset"):
        AirtableClient(
            token=TOKEN,
            base_id=BASE_ID,
            table_id=TABLE_ID,
            managed_field_ids=(FIELD_VLI, FIELD_LI),
            writable_field_ids=(FIELD_VLI, FIELD_LINK),
            transport=QueueTransport([]),
        )


def test_python_bool_does_not_equal_numeric_prevalue() -> None:
    transport = StatefulTransport({"recOne123": {FIELD_VLI: True, FIELD_LI: 2}})

    with pytest.raises(AirtablePreconditionError):
        client(transport).update_records(
            [update("recOne123", (1, 2), (10, 20))], verify_schema=False
        )


def test_no_create_delete_or_upsert_operations_are_exposed() -> None:
    api = client(QueueTransport([]))

    assert not hasattr(api, "create_record")
    assert not hasattr(api, "delete_record")
    assert not hasattr(api, "upsert_records")
