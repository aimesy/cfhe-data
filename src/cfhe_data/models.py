"""Data models for the HCD APR deduplication pipeline."""

from __future__ import annotations

import datetime as dt
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from .normalize import (
    AddressCandidate,
    address_candidates,
    apn_forms,
    identifier,
    normalized_key,
    parse_date,
    parse_number,
    substantive_tracking_id,
)

DEFAULT_CATEGORY_FIELDS: tuple[tuple[str, ...], ...] = (
    ("BP_ACUTELY_LOW_INCOME_DR", "BP_ACUTELY_LOW_INCOME_NDR"),
    ("BP_EXTREMELY_LOW_INCOME_DR", "BP_EXTREMELY_LOW_INCOME_NDR"),
    ("BP_VLOW_INCOME_DR", "BP_VLOW_INCOME_NDR"),
    ("BP_LOW_INCOME_DR", "BP_LOW_INCOME_NDR"),
    ("BP_MOD_INCOME_DR", "BP_MOD_INCOME_NDR"),
    ("BP_ABOVE_MOD_INCOME",),
)


@dataclass(frozen=True, slots=True)
class HcdColumns:
    """Column names used by the HCD Table A2 CSV."""

    report_year: str = "YEAR"
    jurisdiction: str = "JURIS_NAME"
    permit_date: str = "BP_ISSUE_DT1"
    tracking_id: str = "JURS_TRACKING_ID"
    project_name: str = "PROJECT_NAME"
    apn: str = "APN"
    street_address: str = "STREET_ADDRESS"
    standard_address: str = "STD_ADDRESS"
    latitude: str = "LATITUDE"
    longitude: str = "LONGITUDE"
    reported_total: str = "NO_BUILDING_PERMITS"
    unit_category: str = "UNIT_CAT"
    tenure: str = "TENURE"
    category_fields: tuple[tuple[str, ...], ...] = DEFAULT_CATEGORY_FIELDS


DEFAULT_COLUMNS = HcdColumns()


@dataclass(frozen=True, slots=True)
class PermitRecord:
    """One source row prepared for deterministic deduplication."""

    source_index: int
    csv_physical_line: int | None
    report_year: int
    jurisdiction: str
    jurisdiction_display: str
    permit_date: dt.date | None
    tracking_raw: str
    project_raw: str
    raw_apn: str
    formatted_apn: str
    street_address: str
    standard_address: str
    addresses: tuple[AddressCandidate, ...]
    latitude: str
    longitude: str
    categories: tuple[int, ...]
    reported_total: int
    unit_category: str
    tenure: str
    raw_fingerprint: tuple[str, ...]
    raw_header: tuple[str, ...]
    raw_values: tuple[str, ...]

    @property
    def source_record_index(self) -> int:
        """Return the logical record index, where the header is record one."""

        return self.source_index

    @property
    def undated(self) -> bool:
        return self.permit_date is None

    @property
    def raw_row(self) -> dict[str, str]:
        """Return a copy of the complete source row."""

        return dict(zip(self.raw_header, self.raw_values))

    @property
    def tracking_id(self) -> str:
        return substantive_tracking_id(self.tracking_raw)

    @property
    def weak_tracking_id(self) -> str:
        return identifier(self.tracking_raw)

    @property
    def project_key(self) -> str:
        return identifier(self.project_raw)

    @property
    def units(self) -> int:
        return sum(self.categories)

    def as_row(self) -> dict[str, str]:
        """Return a copy of the original source mapping."""

        return self.raw_row

    @classmethod
    def from_mapping(
        cls,
        row: Mapping[str, object],
        *,
        source_index: int,
        csv_physical_line: int | None = None,
        columns: HcdColumns = DEFAULT_COLUMNS,
        fingerprint_fields: Sequence[str] | None = None,
    ) -> PermitRecord:
        """Build a record from one complete CSV row.

        ``fingerprint_fields`` should be the source header when a caller has
        already validated it.  The report year column is always excluded from
        the literal weak ID fingerprint.
        """

        if None in row:
            raise ValueError(
                f"Malformed source row at index {source_index}: extra fields"
            )
        if any(value is None for value in row.values()):
            raise ValueError(
                f"Malformed source row at index {source_index}: missing value"
            )

        required = (
            columns.report_year,
            columns.jurisdiction,
            columns.permit_date,
            columns.tracking_id,
            columns.project_name,
            columns.apn,
            columns.street_address,
            columns.standard_address,
            columns.latitude,
            columns.longitude,
            columns.reported_total,
            columns.unit_category,
            columns.tenure,
            *(field for group in columns.category_fields for field in group),
        )
        missing = sorted({field for field in required if field not in row})
        if missing:
            raise ValueError(
                f"Source row at index {source_index} is missing columns: {', '.join(missing)}"
            )

        def raw(field: str) -> str:
            return str(row[field])

        date_text = raw(columns.permit_date).strip()
        permit_date = parse_date(date_text)
        if date_text and permit_date is None:
            raise ValueError(
                f"Invalid permit date {date_text!r} at source index {source_index}"
            )

        header = (
            tuple(fingerprint_fields) if fingerprint_fields is not None else tuple(row)
        )
        if len(header) != len(set(header)):
            raise ValueError("Source header contains duplicate column names")
        missing_fingerprint_fields = [field for field in header if field not in row]
        if missing_fingerprint_fields:
            raise ValueError(
                "Fingerprint fields are absent from source row: "
                + ", ".join(missing_fingerprint_fields)
            )
        raw_values = tuple(raw(field) for field in header)
        tracking_raw = raw(columns.tracking_id)
        raw_fingerprint = (
            tuple(raw(field) for field in header if field != columns.report_year)
            if not substantive_tracking_id(tracking_raw)
            else ()
        )
        raw_apn, formatted_apn = apn_forms(raw(columns.apn))
        street_address = raw(columns.street_address)
        standard_address = raw(columns.standard_address)
        categories = tuple(
            sum(parse_number(raw(field)) for field in group)
            for group in columns.category_fields
        )
        return cls(
            source_index=source_index,
            csv_physical_line=csv_physical_line,
            report_year=parse_number(raw(columns.report_year)),
            jurisdiction=normalized_key(raw(columns.jurisdiction)),
            jurisdiction_display=raw(columns.jurisdiction).strip(),
            permit_date=permit_date,
            tracking_raw=tracking_raw,
            project_raw=raw(columns.project_name),
            raw_apn=raw_apn,
            formatted_apn=formatted_apn,
            street_address=street_address,
            standard_address=standard_address,
            addresses=address_candidates(street_address, standard_address),
            latitude=raw(columns.latitude).strip(),
            longitude=raw(columns.longitude).strip(),
            categories=categories,
            reported_total=parse_number(raw(columns.reported_total)),
            unit_category=normalized_key(raw(columns.unit_category)),
            tenure=normalized_key(raw(columns.tenure)),
            raw_fingerprint=raw_fingerprint,
            raw_header=header,
            raw_values=raw_values,
        )

    @classmethod
    def from_values(
        cls,
        *,
        source_record_index: int,
        csv_physical_line: int | None,
        report_year: int,
        jurisdiction: str,
        permit_date: dt.date | None,
        tracking_id: str,
        project_name: str,
        apn: str,
        street_address: str,
        standard_address: str,
        latitude: str,
        longitude: str,
        categories: Sequence[int],
        reported_total: int,
        unit_category: str,
        tenure: str,
        raw_row: Mapping[str, object],
        report_year_field: str = "YEAR",
        fingerprint_fields: Sequence[str] | None = None,
        jurisdiction_display: str | None = None,
    ) -> PermitRecord:
        """Build a record from values already parsed by an ingestion layer.

        The complete source mapping remains required because weak ID matching
        depends on a literal fingerprint of every raw field except report year.
        """

        if None in raw_row or any(value is None for value in raw_row.values()):
            raise ValueError(f"Malformed source row at index {source_record_index}")
        header = (
            tuple(fingerprint_fields)
            if fingerprint_fields is not None
            else tuple(raw_row)
        )
        if len(header) != len(set(header)):
            raise ValueError("Source header contains duplicate column names")
        absent = [field for field in header if field not in raw_row]
        if absent:
            raise ValueError(
                "Fingerprint fields are absent from source row: " + ", ".join(absent)
            )
        raw_values = tuple(str(raw_row[field]) for field in header)
        raw_fingerprint = (
            tuple(str(raw_row[field]) for field in header if field != report_year_field)
            if not substantive_tracking_id(tracking_id)
            else ()
        )
        raw_apn, formatted_apn = apn_forms(apn)
        return cls(
            source_index=source_record_index,
            csv_physical_line=csv_physical_line,
            report_year=int(report_year),
            jurisdiction=normalized_key(jurisdiction),
            jurisdiction_display=(jurisdiction_display or jurisdiction).strip(),
            permit_date=permit_date,
            tracking_raw=str(tracking_id),
            project_raw=str(project_name),
            raw_apn=raw_apn,
            formatted_apn=formatted_apn,
            street_address=str(street_address),
            standard_address=str(standard_address),
            addresses=address_candidates(street_address, standard_address),
            latitude=str(latitude).strip(),
            longitude=str(longitude).strip(),
            categories=tuple(int(value) for value in categories),
            reported_total=int(reported_total),
            unit_category=normalized_key(unit_category),
            tenure=normalized_key(tenure),
            raw_fingerprint=raw_fingerprint,
            raw_header=header,
            raw_values=raw_values,
        )


@dataclass(frozen=True, slots=True)
class DedupAuditEntry:
    """One removed row and the exact rule that removed it."""

    source_index: int
    csv_physical_line: int | None
    rule: str
    report_year: int
    retained_report_year: int
    retained_source_indices: tuple[int, ...]
    jurisdiction: str
    permit_date: dt.date | None
    tracking_id: str
    details: tuple[tuple[str, str], ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "source_record_index": self.source_index,
            "csv_physical_line": self.csv_physical_line,
            "rule": self.rule,
            "report_year": self.report_year,
            "retained_report_year": self.retained_report_year,
            "retained_source_indices": list(self.retained_source_indices),
            "jurisdiction": self.jurisdiction,
            "permit_date": self.permit_date.isoformat() if self.permit_date else None,
            "tracking_id": self.tracking_id,
            "details": dict(self.details),
        }


@dataclass(frozen=True, slots=True)
class DedupResult:
    """Retained records, removed records, and the row level audit."""

    retained: tuple[PermitRecord, ...]
    removed: tuple[PermitRecord, ...]
    audit: tuple[DedupAuditEntry, ...]

    @property
    def removed_source_indices(self) -> frozenset[int]:
        return frozenset(record.source_index for record in self.removed)

    def audit_rows(self) -> list[dict[str, object]]:
        return [entry.as_dict() for entry in self.audit]

    def retained_rows(self) -> list[dict[str, str]]:
        return [record.as_row() for record in self.retained]

    def counts_by_rule(self) -> dict[str, int]:
        return dict(sorted(Counter(entry.rule for entry in self.audit).items()))


DedupeResult = DedupResult


def records_from_rows(
    rows: Iterable[Mapping[str, object]],
    *,
    source_index_start: int = 2,
    columns: HcdColumns = DEFAULT_COLUMNS,
    fingerprint_fields: Sequence[str] | None = None,
    csv_physical_lines: Sequence[int | None] | None = None,
) -> tuple[PermitRecord, ...]:
    """Materialize CSV mappings as immutable permit records."""

    materialized = list(rows)
    if not materialized:
        return ()
    if csv_physical_lines is not None and len(csv_physical_lines) != len(materialized):
        raise ValueError("csv_physical_lines must align one for one with rows")
    header = (
        tuple(fingerprint_fields)
        if fingerprint_fields is not None
        else tuple(str(field) for field in materialized[0])
    )
    expected_fields = set(header)
    for offset, row in enumerate(materialized):
        if set(row) != expected_fields:
            source_index = source_index_start + offset
            raise ValueError(f"Source schema changed at index {source_index}")
    return tuple(
        PermitRecord.from_mapping(
            row,
            source_index=source_index_start + offset,
            csv_physical_line=(
                csv_physical_lines[offset] if csv_physical_lines is not None else None
            ),
            columns=columns,
            fingerprint_fields=header,
        )
        for offset, row in enumerate(materialized)
    )
