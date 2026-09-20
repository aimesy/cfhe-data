# visibility: public
"""Regression checks for missing and contradictory Table B sources."""

import hashlib
import json
from copy import deepcopy

import pytest
from test_airtable_sync import CYCLE_POLICY, _source

from cfhe_data.rhna_review import build_table_b_review
from cfhe_data.webflow import canonical_sha256


def sample(tmp_path):
    baseline = _source()
    baseline["jurisdictions"][0].update(
        period_start="2021-10-15",
        period_end="2029-10-15",
        extremely_low=5,
        very_low=15,
        low=30,
        moderate=40,
        above_moderate=160,
        total=250,
    )
    baseline["metadata"]["selected_units"] = 253
    source = tmp_path / "source.pdf"
    source.write_bytes(
        b"test source bytes; no real PDF is needed for a transcribed-source test"
    )
    entry = {
        "jurisdiction_key": "EXAMPLE CITY",
        "format": "reviewed_pdf",
        "page": 7,
        "path": "source.pdf",
        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "url": "https://city.example.gov/apr.pdf",
        "values": {
            "jurisdiction_key": "EXAMPLE CITY",
            "cycle": "6th",
            "reporting_year": 2025,
            "data_through": "2025-12-31",
            "period_start": "2021-10-15",
            "period_end": "2029-10-15",
            "permit_values": {"vli": 20, "li": 30, "mi": 40, "ami": 210},
            "allocation_values": {"vli": 100, "li": 100, "mi": 100, "ami": 300},
            "total": 300,
            "total_allocation": 600,
            "projection_credit": 50,
            "projection_credit_by_income": {"vli": 0, "li": 0, "mi": 0, "ami": 50},
        },
    }
    county_entry = deepcopy(entry)
    county_entry["jurisdiction_key"] = "EXAMPLE COUNTY"
    county_entry["values"].update(
        jurisdiction_key="EXAMPLE COUNTY",
        cycle="7th",
        period_start="2024-06-30",
        period_end="2029-06-30",
        permit_values={"vli": 0, "li": 0, "mi": 0, "ami": 3},
        total=3,
        projection_credit=0,
        projection_credit_by_income={"vli": 0, "li": 0, "mi": 0, "ami": 0},
    )
    manifest = {
        "manifest_version": 1,
        "reporting_year": 2025,
        "sources": [entry, county_entry],
    }
    return baseline, manifest


def review(tmp_path, baseline, manifest, policy=CYCLE_POLICY):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return build_table_b_review(
        baseline=baseline, manifest_path=path, cycle_policy=policy
    )


def test_projection_credit_adds_once_and_pace_uses_reporting_cutoff(tmp_path):
    baseline, manifest = sample(tmp_path)
    result = review(tmp_path, baseline, manifest)
    assert result["ready_for_publication_review"]
    row = result["comparisons"][0]
    assert row["before"]["vli"] == 20
    assert row["total_difference"] == 50
    assert row["difference_beyond_projection_credit"] == 0
    assert row["difference_beyond_projection_by_income"] == {
        "vli": 0,
        "li": 0,
        "mi": 0,
        "ami": 0,
    }
    assert row["pace"]["data_through"] == "2025-12-31"
    assert row["pace"]["minimum_units_for_pace"] == 316
    assert row["pace_by_income"]["vli"]["on_pace"] is False
    assert row["pace_by_income"]["ami"]["on_pace"] is True


def test_missing_table_b_is_not_zero_or_an_a2_fallback(tmp_path):
    baseline, manifest = sample(tmp_path)
    manifest["sources"] = []
    result = review(tmp_path, baseline, manifest)
    assert result["missing_jurisdictions"] == ["EXAMPLE CITY", "EXAMPLE COUNTY"]
    assert result["comparisons"] == []
    assert not result["coverage_complete"]
    assert not result["ready_for_publication_review"]


def test_unexplained_difference_requires_reconciliation(tmp_path):
    baseline, manifest = sample(tmp_path)
    manifest["sources"][0]["values"]["projection_credit"] = 51
    manifest["sources"][0]["values"]["projection_credit_by_income"]["ami"] = 51
    result = review(tmp_path, baseline, manifest)
    assert result["coverage_complete"]
    assert result["unexplained_differences"] == ["EXAMPLE CITY"]
    assert not result["ready_for_publication_review"]


@pytest.mark.parametrize(
    "change",
    [
        "digest",
        "cycle",
        "year",
        "period",
        "missing_band",
        "total",
        "missing_projection_band",
        "projection_total",
    ],
)
def test_invalid_source_stays_an_explicit_error(tmp_path, change):
    baseline, manifest = sample(tmp_path)
    entry = manifest["sources"][0]
    if change == "digest":
        entry["sha256"] = "0" * 64
    elif change == "cycle":
        entry["values"]["cycle"] = "7th"
    elif change == "year":
        entry["values"]["reporting_year"] = 2024
    elif change == "period":
        entry["values"]["period_start"] = "2021-06-30"
    elif change == "missing_band":
        del entry["values"]["permit_values"]["vli"]
    elif change == "missing_projection_band":
        del entry["values"]["projection_credit_by_income"]["vli"]
    elif change == "projection_total":
        entry["values"]["projection_credit"] = 51
    else:
        entry["values"]["total"] = 301
    result = review(tmp_path, baseline, manifest)
    assert len(result["source_errors"]) == 1
    assert not result["coverage_complete"]
    assert [row["jurisdiction_key"] for row in result["comparisons"]] == [
        "EXAMPLE COUNTY"
    ]


def test_does_not_sum_cumulative_tables_from_multiple_years(tmp_path):
    baseline, manifest = sample(tmp_path)
    manifest["sources"].append(dict(manifest["sources"][0]))
    with pytest.raises(ValueError, match="Multiple cumulative Table Bs"):
        review(tmp_path, baseline, manifest)


def test_offsetting_band_differences_still_require_reconciliation(tmp_path):
    baseline, manifest = sample(tmp_path)
    values = manifest["sources"][0]["values"]["permit_values"]
    values["vli"] += 10
    values["li"] -= 10
    result = review(tmp_path, baseline, manifest)
    row = result["comparisons"][0]
    assert row["difference_beyond_projection_credit"] == 0
    assert row["difference_beyond_projection_by_income"] == {
        "vli": 10,
        "li": -10,
        "mi": 0,
        "ami": 0,
    }
    assert result["coverage_complete"]
    assert result["unexplained_differences"] == ["EXAMPLE CITY"]
    assert row["needs_reconciliation"]
    assert not result["ready_for_publication_review"]


def test_empty_baseline_cannot_report_complete_coverage(tmp_path):
    baseline, manifest = sample(tmp_path)
    baseline["jurisdictions"] = []
    baseline["metadata"].update(
        jurisdiction_count=0,
        selected_row_count=0,
        retained_row_count=0,
        selected_units=0,
        cycle_counts={"6th": 0, "7th": 0},
    )
    manifest["sources"] = []
    with pytest.raises(ValueError):
        review(tmp_path, baseline, manifest)


def test_internally_valid_incomplete_baseline_is_rejected(tmp_path):
    baseline, manifest = sample(tmp_path)
    policy = {**CYCLE_POLICY, "expected_jurisdiction_count": 3}
    baseline["metadata"]["cycle_policy_sha256"] = canonical_sha256(policy)
    with pytest.raises(ValueError, match="complete expected jurisdiction coverage"):
        review(tmp_path, baseline, manifest, policy)


def test_baseline_cycle_coverage_must_match_policy(tmp_path):
    baseline, manifest = sample(tmp_path)
    policy = {**CYCLE_POLICY, "expected_cycle_counts": {"6th": 2, "7th": 0}}
    baseline["metadata"]["cycle_policy_sha256"] = canonical_sha256(policy)
    with pytest.raises(ValueError, match="cycle counts disagree"):
        review(tmp_path, baseline, manifest, policy)


@pytest.mark.parametrize("change", ["missing_metadata", "wrong_total", "policy"])
def test_baseline_provenance_and_arithmetic_are_validated(tmp_path, change):
    baseline, manifest = sample(tmp_path)
    if change == "missing_metadata":
        del baseline["metadata"]["source_manifest_sha256"]
    elif change == "wrong_total":
        baseline["jurisdictions"][0]["total"] += 1
    else:
        baseline["metadata"]["cycle_policy_sha256"] = "0" * 64
    with pytest.raises(ValueError):
        review(tmp_path, baseline, manifest)


def test_malformed_workbook_is_a_source_error_in_the_report(tmp_path):
    baseline, manifest = sample(tmp_path)
    source = tmp_path / "broken.xlsx"
    source.write_bytes(b"not an Excel archive")
    manifest["sources"][0].update(
        format="hcd_workbook",
        path=source.name,
        sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
    )
    result = review(tmp_path, baseline, manifest)
    assert result["source_errors"] == [
        {
            "jurisdiction_key": "EXAMPLE CITY",
            "error": "Source is not a readable HCD workbook",
        }
    ]
    assert result["compared_jurisdiction_count"] == 1
    assert not result["coverage_complete"]
    assert not result["ready_for_publication_review"]
