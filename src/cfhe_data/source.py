"""Download HCD source snapshots with stable provenance metadata."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import tomllib
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

USER_AGENT = "cfhe-data/0.1 (+https://github.com/aimesy/cfhe-data)"


class SourceFetchError(RuntimeError):
    """Raised when every configured URL for a source fails."""


@dataclass(frozen=True, slots=True)
class SourceSpec:
    name: str
    package_id: str | None
    resource_id: str
    resource_name: str | None
    resource_api_url: str | None
    datastore_api_url: str | None
    urls: tuple[str, ...]
    schema: str
    minimum_bytes: int


@dataclass(frozen=True, slots=True)
class FetchResult:
    name: str
    resource_id: str
    configured_url: str
    resolved_url: str
    path: Path
    sha256: str
    md5: str
    bytes: int
    etag: str | None
    last_modified: str | None
    content_type: str | None
    retrieved_at: str
    transport: str
    catalog_hash_match: bool | None
    catalog_hash: str | None
    catalog_advertised_size: int | None
    catalog_last_modified: str | None
    datastore_total: int | None
    datastore_fields: tuple[str, ...] | None

    def manifest_entry(self) -> dict[str, object]:
        entry = asdict(self)
        entry.pop("path")
        entry["raw_filename"] = self.path.name
        return entry


def load_specs(path: Path) -> tuple[SourceSpec, ...]:
    with path.open("rb") as handle:
        config = tomllib.load(handle)
    specs: list[SourceSpec] = []
    for name, raw in config.items():
        urls = tuple(str(url) for url in raw.get("urls", ()))
        if not urls:
            raise ValueError(f"Source {name!r} has no URLs")
        specs.append(
            SourceSpec(
                name=name,
                package_id=(str(raw["package_id"]) if raw.get("package_id") else None),
                resource_id=str(raw["resource_id"]),
                resource_name=(
                    str(raw["resource_name"]) if raw.get("resource_name") else None
                ),
                resource_api_url=(
                    str(raw["resource_api_url"])
                    if raw.get("resource_api_url")
                    else None
                ),
                datastore_api_url=(
                    str(raw["datastore_api_url"])
                    if raw.get("datastore_api_url")
                    else None
                ),
                urls=urls,
                schema=str(raw["schema"]),
                minimum_bytes=int(raw["minimum_bytes"]),
            )
        )
    return tuple(specs)


def _header(response: BinaryIO, name: str) -> str | None:
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    value = headers.get(name)
    return str(value) if value is not None else None


def _get_json(
    url: str,
    *,
    opener: Callable[..., BinaryIO],
) -> dict[str, object]:
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
        method="GET",
    )
    try:
        with opener(request, timeout=60) as response:
            value = json.loads(response.read().decode("utf-8"))
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        urllib.error.URLError,
    ) as error:
        raise SourceFetchError(
            f"Unable to read source metadata from {url}: {error}"
        ) from error
    if not isinstance(value, dict) or value.get("success") is not True:
        raise SourceFetchError(
            f"Source metadata endpoint did not return success: {url}"
        )
    result = value.get("result")
    if not isinstance(result, dict):
        raise SourceFetchError(
            f"Source metadata endpoint returned no result object: {url}"
        )
    return result


def _catalog_metadata(
    spec: SourceSpec,
    *,
    opener: Callable[..., BinaryIO],
) -> dict[str, object]:
    if spec.resource_api_url is None:
        return {}
    result = _get_json(spec.resource_api_url, opener=opener)
    if str(result.get("id")) != spec.resource_id:
        raise SourceFetchError(
            f"Catalog returned the wrong resource ID for {spec.name}"
        )
    if spec.package_id and str(result.get("package_id")) != spec.package_id:
        raise SourceFetchError(f"Catalog returned the wrong package ID for {spec.name}")
    if spec.resource_name and str(result.get("name")) != spec.resource_name:
        raise SourceFetchError(f"Catalog resource name changed for {spec.name}")
    if str(result.get("format", "")).upper() != "CSV":
        raise SourceFetchError(f"Catalog resource format is not CSV for {spec.name}")
    if result.get("datastore_active") is not True:
        raise SourceFetchError(f"Catalog DataStore is inactive for {spec.name}")
    return result


def _datastore_metadata(
    spec: SourceSpec,
    *,
    opener: Callable[..., BinaryIO],
) -> tuple[int | None, tuple[str, ...] | None]:
    if spec.datastore_api_url is None:
        return None, None
    result = _get_json(spec.datastore_api_url, opener=opener)
    total = result.get("total")
    fields = result.get("fields")
    if not isinstance(total, int) or total < 0:
        raise SourceFetchError(f"DataStore returned an invalid total for {spec.name}")
    if not isinstance(fields, list):
        raise SourceFetchError(f"DataStore returned no field list for {spec.name}")
    field_names: list[str] = []
    for field in fields:
        if not isinstance(field, Mapping) or not isinstance(field.get("id"), str):
            raise SourceFetchError(
                f"DataStore returned an invalid field for {spec.name}"
            )
        if field["id"] != "_id":
            field_names.append(field["id"])
    return total, tuple(field_names)


def fetch_source(
    spec: SourceSpec,
    raw_dir: Path,
    *,
    opener: Callable[..., BinaryIO] = urllib.request.urlopen,
) -> FetchResult:
    raw_dir.mkdir(parents=True, exist_ok=True)
    catalog = _catalog_metadata(spec, opener=opener)
    datastore_total, datastore_fields = _datastore_metadata(spec, opener=opener)
    failures: list[str] = []
    for configured_url in spec.urls:
        temp_path: Path | None = None
        try:
            request = urllib.request.Request(
                configured_url,
                headers={
                    "Accept": "text/csv,application/octet-stream;q=0.9,*/*;q=0.1",
                    "Accept-Encoding": "identity",
                    "User-Agent": USER_AGENT,
                },
                method="GET",
            )
            with opener(request, timeout=180) as response:
                digest = hashlib.sha256()
                md5_digest = hashlib.md5(usedforsecurity=False)
                byte_count = 0
                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    dir=raw_dir,
                    prefix=f".{spec.name}-",
                    suffix=".part",
                    delete=False,
                ) as temporary:
                    temp_path = Path(temporary.name)
                    while chunk := response.read(1024 * 1024):
                        temporary.write(chunk)
                        digest.update(chunk)
                        md5_digest.update(chunk)
                        byte_count += len(chunk)
                if byte_count < spec.minimum_bytes:
                    raise SourceFetchError(
                        f"{spec.name} returned {byte_count:,} bytes, below the "
                        f"{spec.minimum_bytes:,} byte floor"
                    )
                sha256 = digest.hexdigest()
                md5 = md5_digest.hexdigest()
                catalog_hash = str(catalog.get("hash") or "").strip().lower() or None
                is_datastore_dump = "/datastore/dump/" in configured_url
                catalog_hash_match = None
                if (
                    catalog_hash is not None
                    and len(catalog_hash) == 32
                    and not is_datastore_dump
                ):
                    catalog_hash_match = catalog_hash == md5
                    if not catalog_hash_match:
                        raise SourceFetchError(
                            f"{spec.name} MD5 does not match the CKAN resource hash"
                        )
                destination = raw_dir / f"{spec.name}-{sha256[:16]}.csv"
                if destination.exists():
                    temp_path.unlink()
                else:
                    os.replace(temp_path, destination)
                temp_path = None
                resolved_url = getattr(
                    response,
                    "geturl",
                    lambda configured_url=configured_url: configured_url,
                )()
                return FetchResult(
                    name=spec.name,
                    resource_id=spec.resource_id,
                    configured_url=configured_url,
                    resolved_url=str(resolved_url),
                    path=destination,
                    sha256=sha256,
                    md5=md5,
                    bytes=byte_count,
                    etag=_header(response, "ETag"),
                    last_modified=_header(response, "Last-Modified"),
                    content_type=_header(response, "Content-Type"),
                    retrieved_at=datetime.now(UTC).replace(microsecond=0).isoformat(),
                    transport="datastore_dump" if is_datastore_dump else "uploaded_csv",
                    catalog_hash_match=catalog_hash_match,
                    catalog_hash=catalog_hash,
                    catalog_advertised_size=(
                        int(catalog["size"])
                        if catalog.get("size") is not None
                        else None
                    ),
                    catalog_last_modified=(
                        str(catalog["last_modified"])
                        if catalog.get("last_modified")
                        else None
                    ),
                    datastore_total=datastore_total,
                    datastore_fields=datastore_fields,
                )
        except (OSError, SourceFetchError, urllib.error.URLError) as error:
            failures.append(f"{configured_url}: {type(error).__name__}: {error}")
            if temp_path is not None and temp_path.exists():
                temp_path.unlink()
    joined = "\n".join(failures)
    raise SourceFetchError(
        f"Unable to fetch {spec.name} from any configured URL:\n{joined}"
    )


def stable_manifest(
    results: Sequence[FetchResult],
    *,
    previous: Mapping[str, object] | None = None,
) -> dict[str, object]:
    previous_sources = (previous or {}).get("sources", {})
    if not isinstance(previous_sources, Mapping):
        previous_sources = {}
    sources: dict[str, object] = {}
    for result in sorted(results, key=lambda item: item.name):
        entry = result.manifest_entry()
        prior = previous_sources.get(result.name)
        if isinstance(prior, Mapping) and prior.get("sha256") == result.sha256:
            entry["retrieved_at"] = prior.get("retrieved_at", result.retrieved_at)
        sources[result.name] = entry
    return {"manifest_version": 1, "sources": sources}


def write_manifest(path: Path, manifest: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
