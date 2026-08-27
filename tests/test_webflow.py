from __future__ import annotations

from copy import deepcopy

import pytest

from cfhe_data.webflow import (
    CMSStateChangedError,
    InvalidTotalsError,
    MappingConfigurationError,
    UnknownJurisdictionError,
    build_change_plan,
    verify_change_plan,
)

TOTAL_FIELDS = (
    "acutely_low",
    "extremely_low",
    "very_low",
    "low",
    "moderate",
    "above_moderate",
    "total",
    "undated_permits",
)


def jurisdiction(name: str, start: int) -> dict[str, object]:
    categories = {
        field: start + offset for offset, field in enumerate(TOTAL_FIELDS[:6])
    }
    return {
        "jurisdiction": name,
        "jurisdiction_key": name.upper(),
        **categories,
        "total": sum(categories.values()),
        "undated_permits": 0,
        "last_updated": "2026-08-21",
        "data_status": "reported",
    }


def totals() -> dict[str, object]:
    return {
        "metadata": {
            "cutoff_year": 2025,
            "source_manifest_sha256": "a" * 64,
            "dedupe_profile": "tracking-id-jurisdiction-v1",
            "provisional": False,
            "complete_after": "2026-06-30",
            "jurisdiction_count": 2,
            "selected_row_count": 20,
            "selected_units": 123,
            "retained_row_count": 17,
            "removed_row_count": 3,
            "removed_units": 21,
            "undated_permits": 0,
            "last_updated": "2026-08-21",
        },
        "jurisdictions": [jurisdiction("Alpha", 1), jurisdiction("Beta", 11)],
    }


def config() -> dict[str, object]:
    return {
        "site_id": "site-fixture",
        "collection_id": "collection-fixture",
        "jurisdiction_match_field": "slug",
        "fields": {field: f"cms-{field}" for field in TOTAL_FIELDS},
        "jurisdiction_mappings": {"Alpha": "alpha", "Beta": "beta"},
    }


def cms_item(item_id: str, name: str, start: int) -> dict[str, object]:
    data = {
        "slug": name,
        "unmanaged-note": "must remain outside the plan",
    }
    row = jurisdiction(name, start)
    data.update({f"cms-{field}": row[field] for field in TOTAL_FIELDS})
    return {"id": item_id, "fieldData": data, "isDraft": False}


def snapshot() -> dict[str, object]:
    return {
        "items": [
            cms_item("item-alpha", "alpha", 1),
            cms_item("item-beta", "beta", 11),
        ],
        "pagination": {"limit": 100, "offset": 0, "total": 2},
    }


def test_unchanged_items_are_omitted() -> None:
    plan = build_change_plan(totals(), snapshot(), config())

    assert plan["change_count"] == 0
    assert plan["changes"] == []
    assert plan["source_manifest_sha256"] == "a" * 64
    assert plan["dedupe_profile"] == "tracking-id-jurisdiction-v1"
    assert plan["cutoff_year"] == 2025
    assert plan["provisional"] is False
    verify_change_plan(plan, snapshot())


def test_change_contains_only_configured_fields_and_before_after_values() -> None:
    current = snapshot()
    current["items"][0]["fieldData"]["cms-total"] = 999
    plan = build_change_plan(totals(), current, config())

    assert plan["change_count"] == 1
    change = plan["changes"][0]
    assert change["item_id"] == "item-alpha"
    assert change["jurisdiction"] == "Alpha"
    assert change["fields"] == {
        "cms-total": {"source_field": "total", "before": 999, "after": 21}
    }
    assert "unmanaged-note" not in change["fields"]


def test_jurisdiction_undated_value_takes_precedence_over_metadata_total() -> None:
    source = totals()
    source["jurisdictions"][0]["undated_permits"] = 1
    source["metadata"]["undated_permits"] = 1
    plan = build_change_plan(source, snapshot(), config())

    change = next(item for item in plan["changes"] if item["jurisdiction"] == "Alpha")
    assert change["fields"]["cms-undated_permits"]["after"] == 1


def test_unknown_jurisdiction_is_rejected() -> None:
    current = snapshot()
    current["items"] = current["items"][:1]
    current["pagination"]["total"] = 1

    with pytest.raises(UnknownJurisdictionError, match="Beta"):
        build_change_plan(totals(), current, config())


def test_duplicate_explicit_mapping_is_rejected() -> None:
    mapping = config()
    mapping["jurisdiction_mappings"] = {"Alpha": "alpha", "Beta": "alpha"}

    with pytest.raises(MappingConfigurationError, match="both map"):
        build_change_plan(totals(), snapshot(), mapping)


def test_plan_digest_is_stable_across_input_order() -> None:
    first_totals = totals()
    first_snapshot = snapshot()
    first = build_change_plan(first_totals, first_snapshot, config())

    second_totals = deepcopy(first_totals)
    second_totals["jurisdictions"].reverse()
    second_snapshot = deepcopy(first_snapshot)
    second_snapshot["items"].reverse()
    second = build_change_plan(second_totals, second_snapshot, config())

    assert first == second
    assert first["plan_sha256"] == second["plan_sha256"]


def test_changed_cms_state_invalidates_reviewed_plan() -> None:
    original = snapshot()
    plan = build_change_plan(totals(), original, config())
    changed = deepcopy(original)
    changed["items"][0]["fieldData"]["unmanaged-note"] = "changed elsewhere"

    with pytest.raises(CMSStateChangedError, match="CMS state changed"):
        verify_change_plan(plan, changed)


def test_missing_required_field_mapping_is_rejected() -> None:
    mapping = config()
    del mapping["fields"]["undated_permits"]

    with pytest.raises(MappingConfigurationError, match="undated_permits"):
        build_change_plan(totals(), snapshot(), mapping)


def test_inconsistent_total_is_rejected_before_planning() -> None:
    inconsistent = totals()
    inconsistent["jurisdictions"][0]["total"] += 1

    with pytest.raises(InvalidTotalsError, match="six permit categories"):
        build_change_plan(inconsistent, snapshot(), config())


def test_aggregate_unit_tampering_is_rejected_before_planning() -> None:
    inconsistent = totals()
    inconsistent["jurisdictions"][0]["above_moderate"] += 1
    inconsistent["jurisdictions"][0]["total"] += 1

    with pytest.raises(InvalidTotalsError, match="selected_units minus"):
        build_change_plan(inconsistent, snapshot(), config())


def test_metadata_row_tampering_is_rejected_before_planning() -> None:
    inconsistent = totals()
    inconsistent["metadata"]["retained_row_count"] += 1

    with pytest.raises(InvalidTotalsError, match="row counts do not reconcile"):
        build_change_plan(inconsistent, snapshot(), config())


@pytest.mark.parametrize(
    "field",
    ["selected_units", "retained_row_count", "removed_units", "last_updated"],
)
def test_strict_metadata_fields_are_required_before_planning(field: str) -> None:
    incomplete = totals()
    del incomplete["metadata"][field]

    with pytest.raises(InvalidTotalsError, match=field):
        build_change_plan(incomplete, snapshot(), config())
