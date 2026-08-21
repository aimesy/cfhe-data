"""CSV contracts and strict scalar parsing."""

from __future__ import annotations

import csv
import datetime as dt
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Self

from jsonschema import Draft202012Validator


@dataclass(frozen=True, slots=True)
class CsvContract:
    required_columns: tuple[str, ...]
    unique_columns: bool = True
    allow_additional_columns: bool = True
    ordered_columns: tuple[str, ...] | None = None
    ignored_columns: tuple[str, ...] = ()


def load_contract(path: Path) -> CsvContract:
    raw = json.loads(path.read_text(encoding="utf-8"))
    required = tuple(str(field) for field in raw.get("required_columns", ()))
    if not required:
        raise ValueError(f"CSV contract has no required_columns: {path}")
    return CsvContract(
        required_columns=required,
        unique_columns=bool(raw.get("unique_columns", True)),
        allow_additional_columns=bool(raw.get("allow_additional_columns", True)),
        ordered_columns=(
            tuple(str(field) for field in raw["ordered_columns"])
            if raw.get("ordered_columns") is not None
            else None
        ),
        ignored_columns=tuple(str(field) for field in raw.get("ignored_columns", ())),
    )


def validate_json_document(value: object, schema_path: Path) -> None:
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    errors = sorted(
        validator.iter_errors(value),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    if not errors:
        return
    error = errors[0]
    location = "$"
    for part in error.absolute_path:
        location += f"[{part}]" if isinstance(part, int) else f".{part}"
    raise ValueError(f"JSON schema validation failed at {location}: {error.message}")


def validate_header(
    fieldnames: Sequence[str] | None, contract: CsvContract, source: str
) -> tuple[str, ...]:
    if not fieldnames:
        raise ValueError(f"{source} has no CSV header")
    raw_fields = tuple(fieldnames)
    if contract.unique_columns and len(raw_fields) != len(set(raw_fields)):
        duplicates = sorted(
            {field for field in raw_fields if raw_fields.count(field) > 1}
        )
        raise ValueError(f"{source} has duplicate columns: {', '.join(duplicates)}")
    fields = tuple(
        field for field in raw_fields if field not in contract.ignored_columns
    )
    if contract.ordered_columns is not None and fields != contract.ordered_columns:
        raise ValueError(
            f"{source} ordered header changed: expected {len(contract.ordered_columns)} "
            f"columns, received {len(fields)}"
        )
    missing = sorted(set(contract.required_columns) - set(fields))
    if missing:
        raise ValueError(f"{source} is missing required columns: {', '.join(missing)}")
    if not contract.allow_additional_columns:
        allowed = contract.ordered_columns or contract.required_columns
        additional = sorted(set(fields) - set(allowed))
        if additional:
            raise ValueError(
                f"{source} has unexpected columns: {', '.join(additional)}"
            )
    return fields


class ValidatedCsv:
    """Open a CSV and validate its header before any row is consumed."""

    def __init__(self, path: Path, contract: CsvContract):
        self.path = path
        self.contract = contract
        self._handle = None
        self.reader: csv.DictReader | None = None
        self.fieldnames: tuple[str, ...] = ()

    def __enter__(self) -> Self:
        self._handle = self.path.open(encoding="utf-8-sig", newline="")
        self.reader = csv.DictReader(self._handle)
        self.fieldnames = validate_header(
            self.reader.fieldnames, self.contract, str(self.path)
        )
        return self

    def __exit__(self, *_args) -> None:
        if self._handle is not None:
            self._handle.close()

    def rows(self):
        if self.reader is None:
            raise RuntimeError("CSV is not open")
        for record_index, row in enumerate(self.reader, start=2):
            if None in row or any(value is None for value in row.values()):
                raise ValueError(
                    f"Malformed CSV row at logical record {record_index}, "
                    f"physical line {self.reader.line_num}, source {self.path}"
                )
            for field in self.contract.ignored_columns:
                row.pop(field, None)
            yield record_index, self.reader.line_num, row


def open_validated_csv(path: Path, contract: CsvContract) -> ValidatedCsv:
    return ValidatedCsv(path, contract)


def parse_nonnegative_integer(value: object, *, field: str, record: int) -> int:
    text = str(value or "").strip().replace(",", "")
    if not text:
        return 0
    try:
        parsed = Decimal(text)
    except InvalidOperation as error:
        raise ValueError(
            f"{field} is not numeric at record {record}: {text!r}"
        ) from error
    if not parsed.is_finite() or parsed != parsed.to_integral_value() or parsed < 0:
        raise ValueError(
            f"{field} must be a nonnegative integer at record {record}: {text!r}"
        )
    return int(parsed)


def parse_date(
    value: object, *, field: str, record: int, blank_ok: bool = True
) -> dt.date | None:
    text = str(value or "").strip()
    if not text:
        if blank_ok:
            return None
        raise ValueError(f"{field} is blank at record {record}")
    for date_format in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y", "%Y/%m/%d"):
        try:
            return dt.datetime.strptime(text, date_format).date()  # noqa: DTZ007
        except ValueError:
            continue
    raise ValueError(f"{field} has an invalid date at record {record}: {text!r}")


def parse_planning_period(value: object, *, record: int) -> tuple[dt.date, dt.date]:
    text = str(value or "").strip()
    parts = [part.strip() for part in text.split("-")]
    if len(parts) != 2:
        raise ValueError(f"Planning Period is invalid at record {record}: {text!r}")
    start = parse_date(
        parts[0], field="Planning Period start", record=record, blank_ok=False
    )
    end = parse_date(
        parts[1], field="Planning Period end", record=record, blank_ok=False
    )
    assert start is not None and end is not None
    if start > end:
        raise ValueError(
            f"Planning Period starts after it ends at record {record}: {text!r}"
        )
    return start, end


def require_columns(
    row: Mapping[str, str], fields: Sequence[str], *, record: int
) -> None:
    missing = [field for field in fields if field not in row]
    if missing:
        raise ValueError(f"Record {record} is missing fields: {', '.join(missing)}")
