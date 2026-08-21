from cfhe_data.normalize import (
    address_candidates,
    address_match_kind,
    apn_forms,
    proposed_link_kind,
    substantive_tracking_id,
)


def test_suffixes_and_one_edit_are_normalized() -> None:
    left = address_candidates("100 North Main Street Apt 02", "")
    right = address_candidates("100 N Mainn St Unit 2", "")

    assert address_match_kind(left, right) == 1
    assert left[0] == ("100", "N MAIN ST", (("UNIT", "2"),))


def test_house_and_unit_must_both_match() -> None:
    baseline = address_candidates("100 Main Street Apt 2", "")

    assert (
        address_match_kind(baseline, address_candidates("101 Main St Apt 2", ""))
        is None
    )
    assert (
        address_match_kind(baseline, address_candidates("100 Main St Apt 3", ""))
        is None
    )


def test_apn_does_not_override_two_incompatible_addresses() -> None:
    raw_apn, formatted_apn = apn_forms("001-002-003")

    assert (
        proposed_link_kind(
            address_candidates("100 Main St", ""),
            address_candidates("200 Oak Ave", ""),
            raw_apn,
            raw_apn,
            formatted_apn,
            formatted_apn,
        )
        is None
    )
    assert (
        proposed_link_kind(
            (),
            address_candidates("200 Oak Ave", ""),
            raw_apn,
            raw_apn,
            formatted_apn,
            formatted_apn,
        )
        == 2
    )


def test_generic_tracking_ids_are_not_substantive() -> None:
    assert substantive_tracking_id("N/A") == ""
    assert substantive_tracking_id(" 0 ") == ""
    assert substantive_tracking_id("BP-2024-17") == "BP202417"
