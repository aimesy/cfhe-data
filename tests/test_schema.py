from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from cfhe_data.schema import (
    CsvContract,
    load_contract,
    open_validated_csv,
    parse_date,
    parse_nonnegative_integer,
    parse_planning_period,
    validate_header,
)


def test_header_allows_new_columns_but_rejects_missing_and_duplicate() -> None:
    contract = CsvContract(("A", "B"))

    assert validate_header(("A", "B", "NEW"), contract, "fixture") == ("A", "B", "NEW")
    with pytest.raises(ValueError, match="missing required columns: B"):
        validate_header(("A",), contract, "fixture")
    with pytest.raises(ValueError, match="duplicate columns: A"):
        validate_header(("A", "A", "B"), contract, "fixture")


def test_header_can_pin_exact_order() -> None:
    contract = CsvContract(("A", "B"), ordered_columns=("A", "B"))

    assert validate_header(("A", "B"), contract, "fixture") == ("A", "B")
    with pytest.raises(ValueError, match="ordered header changed"):
        validate_header(("B", "A"), contract, "fixture")


def test_header_can_ignore_only_datastore_synthetic_id(tmp_path: Path) -> None:
    contract = CsvContract(
        ("A", "B"),
        allow_additional_columns=False,
        ordered_columns=("A", "B"),
        ignored_columns=("_id",),
    )
    path = tmp_path / "dump.csv"
    path.write_text("_id,A,B\n7,one,two\n", encoding="utf-8")

    with open_validated_csv(path, contract) as opened:
        rows = list(opened.rows())

    assert opened.fieldnames == ("A", "B")
    assert rows[0][2] == {"A": "one", "B": "two"}


def test_rhna_dump_aliases_preserve_raw_bytes_and_match_uploaded_rows(
    tmp_path: Path,
) -> None:
    contract = load_contract(
        Path(__file__).resolve().parents[1] / "schemas/rhna_progress_6.json"
    )
    aliases = dict(contract.column_aliases)
    inverse_aliases = {label: field_id for field_id, label in aliases.items()}
    fields = contract.ordered_columns
    values = ["Example City", "01/01/2023 - 12/31/2031", "TRUE", *["0"] * 12]
    uploaded = tmp_path / "uploaded.csv"
    uploaded.write_text(
        ",".join(fields) + "\n" + ",".join(values) + "\n", encoding="utf-8"
    )
    dump = tmp_path / "dump.csv"
    dump.write_text(
        "_id,"
        + ",".join(inverse_aliases.get(name, name) for name in fields)
        + "\n1,"
        + ",".join(values)
        + "\n",
        encoding="utf-8",
    )
    original_bytes = dump.read_bytes()

    with open_validated_csv(uploaded, contract) as opened:
        uploaded_rows = [row for _index, _line, row in opened.rows()]
    with open_validated_csv(dump, contract) as opened:
        dump_rows = [row for _index, _line, row in opened.rows()]

    assert dump_rows == uploaded_rows
    assert dump_rows[0]["6th Cycle Started"] == "TRUE"
    assert dump.read_bytes() == original_bytes


@pytest.mark.parametrize(
    "fields, message",
    [
        (("A", "a"), "duplicate columns after alias mapping"),
        (("B", "a"), "ordered header changed"),
        (("unknown_a", "B"), "ordered header changed"),
        (("a", "B", "extra"), "ordered header changed"),
        (("a",), "ordered header changed"),
    ],
)
def test_csv_aliases_keep_exact_schema_checks(fields, message: str) -> None:
    contract = CsvContract(
        ("A", "B"),
        ordered_columns=("A", "B"),
        allow_additional_columns=False,
        column_aliases=(("a", "A"),),
    )
    with pytest.raises(ValueError, match=message):
        validate_header(fields, contract, "fixture")


def test_validated_csv_rejects_short_rows(tmp_path: Path) -> None:
    path = tmp_path / "bad.csv"
    path.write_text("A,B\n1\n", encoding="utf-8")

    with (
        open_validated_csv(path, CsvContract(("A", "B"))) as opened,
        pytest.raises(ValueError, match="Malformed CSV row"),
    ):
        list(opened.rows())


@pytest.mark.parametrize("value", ["-1", "1.5", "nan", "hello"])
def test_count_parser_rejects_non_integer_values(value: str) -> None:
    with pytest.raises(ValueError, match="COUNT"):
        parse_nonnegative_integer(value, field="COUNT", record=7)


def test_count_parser_accepts_blank_and_integral_decimal() -> None:
    assert parse_nonnegative_integer("", field="COUNT", record=1) == 0
    assert parse_nonnegative_integer("1,024.0", field="COUNT", record=1) == 1024


def test_date_parser_does_not_treat_invalid_nonblank_as_missing() -> None:
    assert parse_date("", field="DATE", record=1) is None
    with pytest.raises(ValueError, match="invalid date"):
        parse_date("not-a-date", field="DATE", record=1)


def test_planning_period_is_ordered() -> None:
    assert parse_planning_period("01/31/2023 - 01/31/2031", record=2) == (
        dt.date(2023, 1, 31),
        dt.date(2031, 1, 31),
    )
    with pytest.raises(ValueError, match="starts after"):
        parse_planning_period("01/31/2031 - 01/31/2023", record=2)
