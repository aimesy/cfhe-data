from __future__ import annotations

from cfhe_data.dedupe import RULE_CARRIED_FORWARD, deduplicate_carried_forward
from cfhe_data.models import (
    DEFAULT_CATEGORY_FIELDS,
    ENTITLEMENT_CATEGORY_FIELDS,
    HcdColumns,
    PermitRecord,
)

ENTITLEMENT_COLUMNS = HcdColumns(
    permit_date="ENT_APPROVE_DT1",
    reported_total="NO_ENTITLEMENTS",
    category_fields=ENTITLEMENT_CATEGORY_FIELDS,
)
PERMIT_COLUMNS = HcdColumns(category_fields=DEFAULT_CATEGORY_FIELDS)
PERMIT_PAIR = ("BP_ISSUE_DT1", "NO_BUILDING_PERMITS")
ENTITLEMENT_PAIR = ("ENT_APPROVE_DT1", "NO_ENTITLEMENTS")


def _entitlement(**updates: object) -> dict[str, object]:
    result: dict[str, object] = {
        "YEAR": "2023",
        "JURIS_NAME": "Example City",
        "ENT_APPROVE_DT1": "06/15/2022",
        "NO_ENTITLEMENTS": "137",
        "BP_ISSUE_DT1": "07/13/2023",
        "NO_BUILDING_PERMITS": "137",
        "JURS_TRACKING_ID": "Case No. 2021-36",
        "PROJECT_NAME": "",
        "APN": "1008-361-01",
        "STREET_ADDRESS": "4847-4877 Arrow Highway",
        "STD_ADDRESS": "",
        "LATITUDE": "",
        "LONGITUDE": "",
        "UNIT_CAT": "5+ Units",
        "TENURE": "Rental",
    }
    for groups in (DEFAULT_CATEGORY_FIELDS, ENTITLEMENT_CATEGORY_FIELDS):
        for group in groups:
            for field in group:
                result[field] = "0"
    result["ABOVE_MOD_INCOME"] = "137"
    result["BP_ABOVE_MOD_INCOME"] = "137"
    result.update(updates)
    return result


def _records(*rows: dict[str, object]) -> list[PermitRecord]:
    return [
        PermitRecord.from_mapping(row, source_index=index, columns=ENTITLEMENT_COLUMNS)
        for index, row in enumerate(rows)
    ]


def _moved_pair(**later: object) -> list[PermitRecord]:
    return _records(
        _entitlement(),
        _entitlement(
            YEAR="2025",
            JURS_TRACKING_ID="2021-36",
            APN="1008-361-77",
            STREET_ADDRESS="4855 Arrow Hwy",
            **later,
        ),
    )


def test_permit_date_and_count_link_a_carried_entitlement_that_moved() -> None:
    result = deduplicate_carried_forward(_moved_pair(), corroboration=PERMIT_PAIR)

    assert [record.source_index for record in result.retained] == [1]
    assert result.audit[0].rule == RULE_CARRIED_FORWARD


def test_corroboration_is_off_by_default() -> None:
    assert not deduplicate_carried_forward(_moved_pair()).removed


def test_entitlement_date_and_count_link_a_carried_permit_that_moved() -> None:
    rows = (
        _entitlement(),
        _entitlement(
            YEAR="2025",
            JURS_TRACKING_ID="2021-36",
            APN="1008-361-77",
            STREET_ADDRESS="4855 Arrow Hwy",
        ),
    )
    records = [
        PermitRecord.from_mapping(row, source_index=index, columns=PERMIT_COLUMNS)
        for index, row in enumerate(rows)
    ]

    result = deduplicate_carried_forward(records, corroboration=ENTITLEMENT_PAIR)

    assert [record.source_index for record in result.retained] == [1]
    assert result.audit[0].rule == RULE_CARRIED_FORWARD
    assert not deduplicate_carried_forward(records).removed


def test_a_different_permit_date_does_not_corroborate() -> None:
    records = _moved_pair(BP_ISSUE_DT1="07/14/2023")

    assert not deduplicate_carried_forward(records, corroboration=PERMIT_PAIR).removed


def test_a_different_permit_count_does_not_corroborate() -> None:
    records = _moved_pair(NO_BUILDING_PERMITS="136")

    assert not deduplicate_carried_forward(records, corroboration=PERMIT_PAIR).removed


def test_a_blank_permit_does_not_corroborate() -> None:
    records = _records(
        _entitlement(BP_ISSUE_DT1="", NO_BUILDING_PERMITS="0"),
        _entitlement(
            YEAR="2025",
            APN="1008-361-77",
            STREET_ADDRESS="4855 Arrow Hwy",
            BP_ISSUE_DT1="",
            NO_BUILDING_PERMITS="0",
        ),
    )

    assert not deduplicate_carried_forward(records, corroboration=PERMIT_PAIR).removed


def test_a_small_entitlement_needs_a_location_link() -> None:
    small = {
        "NO_ENTITLEMENTS": "4",
        "ABOVE_MOD_INCOME": "4",
        "NO_BUILDING_PERMITS": "4",
        "BP_ABOVE_MOD_INCOME": "4",
    }
    records = _records(
        _entitlement(**small),
        _entitlement(
            YEAR="2025", APN="1008-361-77", STREET_ADDRESS="4855 Arrow Hwy", **small
        ),
    )

    assert not deduplicate_carried_forward(records, corroboration=PERMIT_PAIR).removed
