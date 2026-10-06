from __future__ import annotations

import hashlib
import io
import json
from dataclasses import replace
from email.message import Message
from pathlib import Path

import pytest

from cfhe_data.schema import load_contract, validate_header
from cfhe_data.source import SourceFetchError, SourceSpec, fetch_source, stable_manifest


class FakeResponse(io.BytesIO):
    def __init__(self, payload: bytes, url: str = "https://resolved.example/data.csv"):
        super().__init__(payload)
        self._url = url
        self.headers = Message()
        self.headers["ETag"] = '"fixture"'
        self.headers["Last-Modified"] = "Thu, 20 Aug 2026 12:00:00 GMT"
        self.headers["Content-Type"] = "text/csv"

    def geturl(self) -> str:
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def spec(
    *, minimum_bytes: int = 1, urls: tuple[str, ...] = ("https://one",)
) -> SourceSpec:
    return SourceSpec(
        name="fixture",
        package_id=None,
        resource_id="resource",
        resource_name=None,
        resource_api_url=None,
        datastore_api_url=None,
        urls=urls,
        schema="schemas/fixture.json",
        minimum_bytes=minimum_bytes,
    )


def test_fetch_source_hashes_and_names_immutable_snapshot(tmp_path: Path) -> None:
    payload = b"A,B\n1,2\n"
    result = fetch_source(
        spec(), tmp_path, opener=lambda *_args, **_kwargs: FakeResponse(payload)
    )

    assert result.sha256 == hashlib.sha256(payload).hexdigest()
    assert result.md5 == hashlib.md5(payload, usedforsecurity=False).hexdigest()
    assert result.path.read_bytes() == payload
    assert result.path.name == f"fixture-{result.sha256[:16]}.csv"
    assert result.etag == '"fixture"'


def test_fetch_source_falls_back_to_next_url(tmp_path: Path) -> None:
    calls: list[str] = []

    def opener(request, **_kwargs):
        calls.append(request.full_url)
        if request.full_url == "https://one":
            raise OSError("first source unavailable")
        return FakeResponse(b"A\n1\n")

    result = fetch_source(
        spec(urls=("https://one", "https://two")), tmp_path, opener=opener
    )

    assert calls == ["https://one", "https://two"]
    assert result.configured_url == "https://two"


def test_fetch_source_rejects_suspiciously_small_response(tmp_path: Path) -> None:
    with pytest.raises(SourceFetchError, match="below the"):
        fetch_source(
            spec(minimum_bytes=100),
            tmp_path,
            opener=lambda *_args, **_kwargs: FakeResponse(b"error page"),
        )
    assert not list(tmp_path.glob("*.part"))


def test_stable_manifest_preserves_retrieval_time_for_same_hash(tmp_path: Path) -> None:
    result = fetch_source(
        spec(), tmp_path, opener=lambda *_args, **_kwargs: FakeResponse(b"A\n")
    )
    previous = {
        "sources": {
            "fixture": {
                "sha256": result.sha256,
                "retrieved_at": "2026-08-20T00:00:00+00:00",
            }
        }
    }

    manifest = stable_manifest([result], previous=previous)

    assert manifest["sources"]["fixture"]["retrieved_at"] == "2026-08-20T00:00:00+00:00"


def fetch_with_datastore(tmp_path: Path, fields: list[dict[str, object]]):
    def opener(request, **_kwargs):
        if request.full_url == "https://metadata":
            return FakeResponse(
                json.dumps(
                    {"success": True, "result": {"total": 539, "fields": fields}}
                ).encode("utf-8")
            )
        return FakeResponse(b"A,B\n1,2\n")

    return fetch_source(
        replace(spec(), datastore_api_url="https://metadata"), tmp_path, opener=opener
    )


@pytest.mark.parametrize("use_labels", [False, True])
def test_datastore_matches_pinned_rhna_labels_and_preserves_storage_ids(
    tmp_path: Path, use_labels: bool
) -> None:
    contract = load_contract(
        Path(__file__).resolve().parents[1] / "schemas/rhna_progress_6.json"
    )
    aliases = {
        "6th Cycle Started": "unsafe_6th_cycle_started",
        "VLI %": "vli__",
        "LI %": "li__",
        "MOD %": "mod__",
        "ABOVE MOD %": "above_mod__",
    }
    fields = [{"id": "_id", "type": "int"}]
    for name in contract.ordered_columns:
        field = {"id": aliases.get(name, name) if use_labels else name}
        if use_labels:
            field["info"] = {"label": name}
        fields.append(field)

    result = fetch_with_datastore(tmp_path, fields)

    assert result.datastore_total == 539
    assert validate_header(result.datastore_fields, contract, "DataStore") == (
        contract.ordered_columns
    )
    assert result.datastore_field_ids == tuple(field["id"] for field in fields[1:])
    entry = result.manifest_entry()
    assert entry["datastore_fields"] == contract.ordered_columns
    assert entry["datastore_field_ids"] == result.datastore_field_ids


@pytest.mark.parametrize(
    "fields, message",
    [
        ([{"id": "A", "info": {"label": 7}}], "invalid field label"),
        ([{"id": "A", "info": {"label": ""}}], "invalid field label"),
        ([{"id": "A", "info": {"label": " "}}], "invalid field label"),
        ([{"id": "A", "info": {"label": None}}], "invalid field label"),
        ([{"id": "A", "info": ["invalid"]}], "invalid field info"),
        ([{"id": "A", "info": None}], "invalid field info"),
        ([{"id": "", "info": {"label": "A"}}], "invalid field"),
        ([{"id": "_id"}, {"id": "_id"}], "duplicate fields"),
        ([{"id": "A"}, {"id": "A"}], "duplicate fields"),
        (
            [{"id": "a", "info": {"label": "A"}}, {"id": "A"}],
            "duplicate fields",
        ),
    ],
)
def test_datastore_rejects_invalid_or_ambiguous_fields(
    tmp_path: Path, fields: list[dict[str, object]], message: str
) -> None:
    with pytest.raises(SourceFetchError, match=message):
        fetch_with_datastore(tmp_path, fields)
