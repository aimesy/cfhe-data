"""Normalization primitives for HCD APR permit records.

The functions in this module are deterministic and have no file or network
side effects.  They intentionally preserve a narrow matching boundary: an APN
cannot rescue two populated but incompatible addresses in the conservative
linkage rule.
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from typing import Final

BAD_TRACKING_IDS: Final[frozenset[str]] = frozenset(
    {
        "",
        "0",
        "ADU",
        "MULTIPLE",
        "NA",
        "NONE",
        "NULL",
        "SFR",
        "TBD",
        "UNKNOWN",
        "VARIOUS",
    }
)

BAD_APNS: Final[frozenset[str]] = frozenset(
    {
        "",
        "0",
        "NA",
        "NONE",
        "NULL",
        "TBD",
        "UNKNOWN",
        "NOTAVAILABLE",
        "NOTAPPLICABLE",
        "UNASSIGNED",
    }
)

GENERIC_PROJECTS: Final[frozenset[str]] = frozenset(
    {
        "",
        "0",
        "ADU",
        "MULTIPLE",
        "NA",
        "NONE",
        "NULL",
        "SFR",
        "TBD",
        "UNKNOWN",
        "VARIOUS",
        "SINGLEFAMILY",
        "SINGLEFAMILYRESIDENCE",
    }
)

TOKEN_MAP: Final[dict[str, str]] = {
    "NORTH": "N",
    "SOUTH": "S",
    "EAST": "E",
    "WEST": "W",
    "NORTHEAST": "NE",
    "NORTHWEST": "NW",
    "SOUTHEAST": "SE",
    "SOUTHWEST": "SW",
    "STREET": "ST",
    "STR": "ST",
    "ST": "ST",
    "AVENUE": "AVE",
    "AV": "AVE",
    "AVE": "AVE",
    "ROAD": "RD",
    "RD": "RD",
    "DRIVE": "DR",
    "DRV": "DR",
    "DR": "DR",
    "LANE": "LN",
    "LN": "LN",
    "COURT": "CT",
    "CT": "CT",
    "PLACE": "PL",
    "PL": "PL",
    "BOULEVARD": "BLVD",
    "BLVD": "BLVD",
    "HIGHWAY": "HWY",
    "HWY": "HWY",
    "PARKWAY": "PKWY",
    "PKWY": "PKWY",
    "TERRACE": "TER",
    "TER": "TER",
    "CIRCLE": "CIR",
    "CIR": "CIR",
    "TRAIL": "TRL",
    "TRL": "TRL",
    "PLAZA": "PLZ",
    "PLZ": "PLZ",
    "WAY": "WAY",
    "WY": "WAY",
    "SQUARE": "SQ",
    "SQ": "SQ",
    "LOOP": "LOOP",
}

CANONICAL_SUFFIXES: Final[frozenset[str]] = frozenset(
    {
        "ST",
        "AVE",
        "RD",
        "DR",
        "LN",
        "CT",
        "PL",
        "BLVD",
        "HWY",
        "PKWY",
        "TER",
        "CIR",
        "TRL",
        "PLZ",
        "WAY",
        "SQ",
        "LOOP",
    }
)

UNIT_TYPE_MAP: Final[dict[str, str]] = {
    "UNIT": "UNIT",
    "APT": "UNIT",
    "APARTMENT": "UNIT",
    "SUITE": "UNIT",
    "STE": "UNIT",
    "LOT": "LOT",
    "BLDG": "BLDG",
    "BUILDING": "BLDG",
}

UNIT_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b(UNIT|APT|APARTMENT|SUITE|STE|LOT|BLDG|BUILDING)"
    r"\s*:?[# ]*([A-Z0-9-]+)"
)
UNIT_MARKER_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b(?:UNIT|APT|APARTMENT|SUITE|STE|LOT|BLDG|BUILDING)\b|#"
)

AddressCandidate = tuple[str, str, tuple[tuple[str, str], ...]]


def normalized_text(value: object) -> str:
    """Return Unicode normalized, uppercase text without outer whitespace."""

    return unicodedata.normalize("NFKC", str(value or "")).upper().strip()


def normalized_key(value: object) -> str:
    """Return normalized text with runs of whitespace collapsed."""

    return " ".join(normalized_text(value).split())


def identifier(value: object) -> str:
    """Return an uppercase identifier containing only ASCII letters and digits."""

    return re.sub(r"[^A-Z0-9]", "", normalized_text(value))


def substantive_tracking_id(value: object) -> str:
    """Return a usable jurisdiction tracking ID, or an empty string."""

    tracking_id = identifier(value)
    if len(tracking_id) < 3 or tracking_id in BAD_TRACKING_IDS:
        return ""
    return tracking_id


def parse_number(value: object) -> int:
    """Parse the integer shaped numeric fields in the HCD CSV."""

    text = str(value or "").strip().replace(",", "")
    return int(float(text)) if text else 0


def parse_date(value: object) -> dt.date | None:
    """Parse the date formats observed in HCD APR downloads."""

    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    for date_format in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y", "%Y/%m/%d"):
        try:
            return dt.datetime.strptime(text, date_format).date()  # noqa: DTZ007
        except ValueError:
            continue
    return None


def normalize_unit_value(value: str) -> str:
    stripped = value.lstrip("0")
    return stripped or "0"


def record_unit_signature(
    street_address: object,
    standard_address: object,
) -> set[tuple[str, str]]:
    text = normalized_text(f"{street_address or ''} {standard_address or ''}")
    units: set[tuple[str, str]] = set()
    for match in UNIT_PATTERN.finditer(text):
        units.add(
            (
                UNIT_TYPE_MAP[match.group(1)],
                normalize_unit_value(match.group(2)),
            )
        )
    return units


def address_candidate(
    value: object,
    base_units: set[tuple[str, str]],
) -> AddressCandidate | None:
    text = normalized_text(value).replace("\r", "\n")
    if not text:
        return None
    text = text.split("\n", 1)[0].split(",", 1)[0]
    marker = UNIT_MARKER_PATTERN.search(text)
    if marker:
        text = text[: marker.start()]
    tokens = [TOKEN_MAP.get(token, token) for token in re.findall(r"[A-Z0-9]+", text)]
    if len(tokens) < 2 or not any(character.isdigit() for character in tokens[0]):
        return None

    units = set(base_units)
    if (
        len(tokens) >= 3
        and tokens[-2] in CANONICAL_SUFFIXES
        and re.fullmatch(r"[A-Z]|\d{1,4}", tokens[-1])
    ):
        units.add(("UNIT", normalize_unit_value(tokens.pop())))

    house = tokens[0]
    street = " ".join(tokens[1:])
    if not street:
        return None
    return house, street, tuple(sorted(units))


def address_candidates(
    street_address: object,
    standard_address: object,
) -> tuple[AddressCandidate, ...]:
    """Return distinct normalized address candidates from both HCD fields."""

    units = record_unit_signature(street_address, standard_address)
    candidates = {
        candidate
        for candidate in (
            address_candidate(street_address, units),
            address_candidate(standard_address, units),
        )
        if candidate is not None
    }
    return tuple(sorted(candidates))


def apn_forms(value: object) -> tuple[str, str]:
    """Return compact and zero normalized APN forms, excluding placeholders."""

    raw = identifier(value)
    if raw in BAD_APNS:
        return "", ""
    parts = re.findall(r"[A-Z]+|\d+", raw)
    formatted_parts = [str(int(part)) if part.isdigit() else part for part in parts]
    return raw, "|".join(formatted_parts)


def within_one_edit(left: str, right: str) -> bool:
    """Return whether ordinary character Levenshtein distance is at most one."""

    if left == right:
        return True
    if abs(len(left) - len(right)) > 1:
        return False
    if len(left) == len(right):
        return sum(a != b for a, b in zip(left, right)) <= 1
    if len(left) > len(right):
        left, right = right, left
    left_index = 0
    right_index = 0
    skipped = False
    while left_index < len(left) and right_index < len(right):
        if left[left_index] == right[right_index]:
            left_index += 1
            right_index += 1
            continue
        if skipped:
            return False
        skipped = True
        right_index += 1
    return True


def address_match_kind(
    left: tuple[AddressCandidate, ...],
    right: tuple[AddressCandidate, ...],
) -> int | None:
    """Return 0 for exact, 1 for one edit, or ``None`` for incompatibility."""

    for left_address in left:
        for right_address in right:
            if (
                left_address[0] != right_address[0]
                or left_address[2] != right_address[2]
            ):
                continue
            left_street = left_address[1].replace(" ", "")
            right_street = right_address[1].replace(" ", "")
            if left_street == right_street:
                return 0
            if min(len(left_street), len(right_street)) >= 4 and within_one_edit(
                left_street, right_street
            ):
                return 1
    return None


def proposed_link_kind(
    left_addresses: tuple[AddressCandidate, ...],
    right_addresses: tuple[AddressCandidate, ...],
    left_raw_apn: str,
    right_raw_apn: str,
    left_formatted_apn: str,
    right_formatted_apn: str,
) -> int | None:
    """Return the conservative link kind for two records.

    Kinds 0 and 1 are exact and one edit address links.  Kinds 2 and 3 are
    compact and zero normalized APN links.  APN evidence is considered only
    when at least one record lacks a usable address.
    """

    if left_addresses and right_addresses:
        return address_match_kind(left_addresses, right_addresses)
    if left_raw_apn and left_raw_apn == right_raw_apn:
        return 2
    if left_formatted_apn and left_formatted_apn == right_formatted_apn:
        return 3
    return None


def street_tokens(value: object) -> tuple[str, ...]:
    """Return normalized street tokens without a unit suffix."""

    text = normalized_text(value).replace("\r", "\n")
    text = text.split("\n", 1)[0].split(",", 1)[0]
    marker = UNIT_MARKER_PATTERN.search(text)
    if marker:
        text = text[: marker.start()]
    return tuple(
        TOKEN_MAP.get(token, token) for token in re.findall(r"[A-Z0-9]+", text)
    )
