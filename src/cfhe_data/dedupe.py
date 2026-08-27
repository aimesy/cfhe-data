"""Auditable deduplication for HCD APR permit snapshots.

The public entry points are :func:`deduplicate_records` and
:func:`deduplicate_rows`.  Every removed row receives exactly one named audit
entry.  Ambiguous rows and every row in the selected latest APR year remain.
"""

from __future__ import annotations

import datetime as dt
import re
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from itertools import combinations

from .models import (
    DEFAULT_COLUMNS,
    DedupAuditEntry,
    DedupeResult,
    DedupResult,
    HcdColumns,
    PermitRecord,
    records_from_rows,
)
from .normalize import (
    GENERIC_PROJECTS,
    address_match_kind,
    proposed_link_kind,
    street_tokens,
    within_one_edit,
)

RULE_STRONG_LINKED = "strong_linked_snapshot"
RULE_WEAK_LITERAL = "weak_literal_snapshot"
RULE_STRONG_RELIABLE = "strong_reliable_snapshot"
RULE_STRONG_SPLIT = "strong_split_increment"
RULE_STRONG_AGGREGATE = "strong_aggregate_exception"
RULE_STRONG_REUSED = "strong_reused_id_exception"
RULE_WEAK_PROJECT = "weak_project_snapshot"
RULE_WEAK_DUAL_LOCATION = "weak_dual_location_snapshot"
RULE_WEAK_PROJECT_AND_DUAL = "weak_project_and_dual_location_snapshot"
RULE_TRANSFORMED_LOCATION = "weak_house_number_in_apn_transform"

RULES: tuple[str, ...] = (
    RULE_STRONG_LINKED,
    RULE_WEAK_LITERAL,
    RULE_STRONG_RELIABLE,
    RULE_STRONG_SPLIT,
    RULE_STRONG_AGGREGATE,
    RULE_STRONG_REUSED,
    RULE_WEAK_PROJECT,
    RULE_WEAK_DUAL_LOCATION,
    RULE_WEAK_PROJECT_AND_DUAL,
    RULE_TRANSFORMED_LOCATION,
)

StrongBlockKey = tuple[str, dt.date, str]
IdKey = tuple[str, str]
EdgeFunction = Callable[[int, int], int | None]


@dataclass(frozen=True, slots=True)
class _ComponentAudit:
    matched_components: tuple[tuple[int, ...], ...]
    all_components: tuple[tuple[int, ...], ...]
    rejected_edges: int

    @property
    def is_split(self) -> bool:
        return bool(self.matched_components) and len(self.all_components) > 1


@dataclass(frozen=True, slots=True)
class _CandidateRemoval:
    retained_year: int
    retained_indices: tuple[int, ...]
    evidence: str


def _components_compatible(
    left_members: set[int],
    right_members: set[int],
    records: Sequence[PermitRecord],
) -> bool:
    """Reject a bridge if any populated address pair is incompatible."""

    for left_index in left_members:
        left = records[left_index]
        if not left.addresses:
            continue
        for right_index in right_members:
            right = records[right_index]
            if (
                right.addresses
                and address_match_kind(left.addresses, right.addresses) is None
            ):
                return False
    return True


def _safe_components(
    indices: Sequence[int],
    records: Sequence[PermitRecord],
    edge_function: EdgeFunction,
) -> _ComponentAudit:
    if not indices:
        return _ComponentAudit((), (), 0)
    parents = {index: index for index in indices}
    members = {index: {index} for index in indices}

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    edges: list[tuple[int, int, int, int, int]] = []
    for left_index, right_index in combinations(indices, 2):
        kind = edge_function(left_index, right_index)
        if kind is not None:
            left_source = records[left_index].source_index
            right_source = records[right_index].source_index
            edges.append(
                (
                    kind,
                    min(left_source, right_source),
                    max(left_source, right_source),
                    left_index,
                    right_index,
                )
            )
    edges.sort()

    rejected_edges = 0
    for _kind, _left_source, _right_source, left_index, right_index in edges:
        left_root = find(left_index)
        right_root = find(right_index)
        if left_root == right_root:
            continue
        if not _components_compatible(members[left_root], members[right_root], records):
            rejected_edges += 1
            continue
        if len(members[left_root]) < len(members[right_root]):
            left_root, right_root = right_root, left_root
        parents[right_root] = left_root
        members[left_root].update(members.pop(right_root))

    grouped: dict[int, list[int]] = defaultdict(list)
    for index in indices:
        grouped[find(index)].append(index)
    all_components = tuple(
        tuple(sorted(component, key=lambda index: records[index].source_index))
        for component in sorted(
            grouped.values(),
            key=lambda values: min(records[index].source_index for index in values),
        )
    )
    matched = tuple(
        component
        for component in all_components
        if len({records[index].report_year for index in component}) > 1
    )
    return _ComponentAudit(matched, all_components, rejected_edges)


def _valid_apn(record: PermitRecord) -> bool:
    raw = record.raw_apn
    if not raw or re.fullmatch(r"\d+E[+-]?\d+", raw):
        return False
    digits = re.sub(r"\D", "", raw)
    return not (digits and not re.search(r"[A-Z]", raw) and len(set(digits)) == 1)


def _valid_coordinates(record: PermitRecord) -> tuple[float, float] | None:
    try:
        latitude = float(record.latitude)
        longitude = float(record.longitude)
    except (TypeError, ValueError):
        return None
    if not (32.0 <= latitude <= 42.1 and -125.0 <= longitude <= -114.0):
        return None
    return latitude, longitude


def _cross_year_pairs(
    indices: Sequence[int],
    records: Sequence[PermitRecord],
) -> list[tuple[PermitRecord, PermitRecord]]:
    latest_year = max(records[index].report_year for index in indices)
    latest = [
        records[index] for index in indices if records[index].report_year == latest_year
    ]
    earlier = [
        records[index] for index in indices if records[index].report_year != latest_year
    ]
    return [(left, right) for left in earlier for right in latest]


def _street_evidence(left: PermitRecord, right: PermitRecord) -> bool:
    for left_address in left.addresses:
        for right_address in right.addresses:
            left_street = left_address[1].replace(" ", "")
            right_street = right_address[1].replace(" ", "")
            if left_street == right_street:
                return True
            if min(len(left_street), len(right_street)) >= 5 and within_one_edit(
                left_street, right_street
            ):
                return True
    return False


def _house_evidence(left: PermitRecord, right: PermitRecord) -> bool:
    return any(
        left_address[0] == right_address[0]
        for left_address in left.addresses
        for right_address in right.addresses
    )


def _unit_conflict(left: PermitRecord, right: PermitRecord) -> bool:
    left_units = {unit for address in left.addresses for unit in address[2]}
    right_units = {unit for address in right.addresses for unit in address[2]}
    return bool(left_units and right_units and left_units != right_units)


def _latest_count_guard(
    indices: Sequence[int], records: Sequence[PermitRecord]
) -> bool:
    counts: dict[int, int] = defaultdict(int)
    for index in indices:
        counts[records[index].report_year] += 1
    years = sorted(counts)
    return len(years) > 1 and counts[years[-1]] >= max(
        counts[year] for year in years[:-1]
    )


def _strong_pair_signals(left: PermitRecord, right: PermitRecord) -> tuple[str, ...]:
    exact_apn = (
        _valid_apn(left)
        and _valid_apn(right)
        and left.formatted_apn == right.formatted_apn
    )
    exact_coordinates = _valid_coordinates(left) is not None and _valid_coordinates(
        left
    ) == _valid_coordinates(right)
    exact_substantive_project = (
        bool(left.project_key)
        and left.project_key not in GENERIC_PROJECTS
        and left.project_key == right.project_key
    )
    return tuple(
        name
        for name, present in (
            ("exact_valid_apn", exact_apn),
            ("exact_valid_coordinates", exact_coordinates),
            ("substantive_project", exact_substantive_project),
            ("street", _street_evidence(left, right)),
            ("house_number", _house_evidence(left, right)),
        )
        if present
    )


def _strong_pair_evidence(left: PermitRecord, right: PermitRecord) -> tuple[str, ...]:
    if _unit_conflict(left, right):
        return ()
    evidence = _strong_pair_signals(left, right)
    text_evidence = {
        "substantive_project",
        "street",
        "house_number",
    }.intersection(evidence)
    if {"exact_valid_apn", "exact_valid_coordinates"}.intersection(evidence):
        return evidence
    return evidence if len(text_evidence) >= 2 else ()


def _collision_ids(
    blocks: Mapping[StrongBlockKey, list[int]],
    records: Sequence[PermitRecord],
) -> set[IdKey]:
    collisions: set[IdKey] = set()
    for (jurisdiction, _permit_date, tracking_id), indices in blocks.items():
        by_year: dict[int, list[int]] = defaultdict(list)
        for index in indices:
            by_year[records[index].report_year].append(index)
        for year_indices in by_year.values():
            for left_index, right_index in combinations(year_indices, 2):
                left = records[left_index]
                right = records[right_index]
                if left.addresses and right.addresses:
                    incompatible = (
                        address_match_kind(left.addresses, right.addresses) is None
                    )
                else:
                    incompatible = bool(
                        left.formatted_apn
                        and right.formatted_apn
                        and left.formatted_apn != right.formatted_apn
                    )
                if incompatible:
                    collisions.add((jurisdiction, tracking_id))
                    break
            if (jurisdiction, tracking_id) in collisions:
                break
    return collisions


def _sum_categories(
    indices: Sequence[int],
    records: Sequence[PermitRecord],
) -> tuple[int, ...]:
    if not indices:
        return ()
    width = len(records[indices[0]].categories)
    if any(len(records[index].categories) != width for index in indices):
        raise ValueError("Category vectors have inconsistent widths")
    return tuple(
        sum(records[index].categories[position] for index in indices)
        for position in range(width)
    )


def _same_street_family(
    anchor_index: int,
    other_indices: Sequence[int],
    records: Sequence[PermitRecord],
) -> bool:
    return all(
        _street_evidence(records[anchor_index], records[index])
        for index in other_indices
    )


def _aggregate_exception_qualifies(
    key: StrongBlockKey,
    indices: Sequence[int],
    id_dates: Mapping[IdKey, set[dt.date]],
    records: Sequence[PermitRecord],
) -> bool:
    jurisdiction, _permit_date, tracking_id = key
    if len(id_dates[(jurisdiction, tracking_id)]) != 1:
        return False
    by_year: dict[int, list[int]] = defaultdict(list)
    for index in indices:
        by_year[records[index].report_year].append(index)
    years = sorted(by_year)
    if len(years) != 2:
        return False
    earlier = by_year[years[0]]
    latest = by_year[years[1]]
    if len(earlier) != 1 or len(latest) <= 1:
        return False
    if _sum_categories(earlier, records) != _sum_categories(latest, records):
        return False
    return _same_street_family(earlier[0], latest, records)


def _reused_id_exception_qualifies(
    key: StrongBlockKey,
    indices: Sequence[int],
    id_dates: Mapping[IdKey, set[dt.date]],
    records: Sequence[PermitRecord],
) -> bool:
    jurisdiction, _permit_date, tracking_id = key
    if len(id_dates[(jurisdiction, tracking_id)]) <= 1:
        return False
    by_year: dict[int, list[int]] = defaultdict(list)
    for index in indices:
        by_year[records[index].report_year].append(index)
    if len(by_year) < 2 or any(
        len(year_indices) != 1 for year_indices in by_year.values()
    ):
        return False
    return any(
        _valid_apn(left)
        and _valid_apn(right)
        and left.formatted_apn == right.formatted_apn
        and _valid_coordinates(left) is not None
        and _valid_coordinates(left) == _valid_coordinates(right)
        and not _unit_conflict(left, right)
        for left, right in _cross_year_pairs(indices, records)
    )


def _weak_metadata(record: PermitRecord) -> tuple[object, ...]:
    return (
        record.categories,
        record.reported_total,
        record.unit_category,
        record.tenure,
    )


def _weak_location_match(left: PermitRecord, right: PermitRecord) -> bool:
    if left.addresses and right.addresses:
        return address_match_kind(left.addresses, right.addresses) is not None
    return bool(
        _valid_apn(left)
        and _valid_apn(right)
        and (left.raw_apn == right.raw_apn or left.formatted_apn == right.formatted_apn)
    )


def _weak_dual_location_match(left: PermitRecord, right: PermitRecord) -> bool:
    return bool(
        _valid_apn(left)
        and _valid_apn(right)
        and left.raw_apn == right.raw_apn
        and left.addresses
        and right.addresses
        and address_match_kind(left.addresses, right.addresses) is not None
    )


def _weak_candidate_removals(
    indices: Sequence[int],
    records: Sequence[PermitRecord],
    *,
    require_substantive_project: bool,
    require_dual_location: bool,
) -> dict[int, _CandidateRemoval]:
    blocks: dict[tuple[str, dt.date, str], list[int]] = defaultdict(list)
    for index in indices:
        record = records[index]
        if record.permit_date is None:
            continue
        project_key = record.project_key
        if require_substantive_project and (
            not project_key or project_key in GENERIC_PROJECTS
        ):
            continue
        blocks[(record.jurisdiction, record.permit_date, project_key)].append(index)

    removals: dict[int, _CandidateRemoval] = {}
    for block_indices in blocks.values():

        def edge(left_index: int, right_index: int) -> int | None:
            left = records[left_index]
            right = records[right_index]
            if left.report_year == right.report_year or _weak_metadata(
                left
            ) != _weak_metadata(right):
                return None
            if require_dual_location:
                return 0 if _weak_dual_location_match(left, right) else None
            return 0 if _weak_location_match(left, right) else None

        component_audit = _safe_components(block_indices, records, edge)
        for component in component_audit.matched_components:
            by_year: dict[int, list[int]] = defaultdict(list)
            for index in component:
                by_year[records[index].report_year].append(index)
            latest_year = max(by_year)
            latest_indices = tuple(sorted(by_year[latest_year]))
            earlier_counts = [
                len(values) for year, values in by_year.items() if year != latest_year
            ]
            if len(latest_indices) < max(earlier_counts):
                continue
            evidence = (
                "exact_valid_apn_and_address"
                if require_dual_location
                else "project_and_compatible_location"
            )
            for year, year_indices in by_year.items():
                if year == latest_year:
                    continue
                for index in year_indices:
                    removals[index] = _CandidateRemoval(
                        latest_year, latest_indices, evidence
                    )
    return removals


def house_number_moved_from_apn_to_address(
    earlier: PermitRecord,
    latest: PermitRecord,
) -> bool:
    """Return whether a prior row stored the later house number in APN."""

    if earlier.addresses or not re.fullmatch(r"\d{1,6}", earlier.raw_apn):
        return False
    if not _valid_apn(latest) or latest.raw_apn == earlier.raw_apn:
        return False
    house = earlier.raw_apn.lstrip("0") or "0"
    earlier_signatures = {
        tokens
        for tokens in (
            street_tokens(earlier.street_address),
            street_tokens(earlier.standard_address),
        )
        if tokens
    }
    if not earlier_signatures:
        return False
    for value in (latest.street_address, latest.standard_address):
        tokens = street_tokens(value)
        if len(tokens) < 2:
            continue
        latest_house = tokens[0].lstrip("0") or "0"
        if latest_house == house and tokens[1:] in earlier_signatures:
            return True
    return False


def _audit_entry(
    record: PermitRecord,
    *,
    rule: str,
    retained_year: int,
    retained_indices: Sequence[int],
    records: Sequence[PermitRecord],
    details: tuple[tuple[str, str], ...] = (),
) -> DedupAuditEntry:
    matched_source_indices = tuple(
        records[index].source_index for index in retained_indices
    )
    return DedupAuditEntry(
        source_index=record.source_index,
        csv_physical_line=record.csv_physical_line,
        rule=rule,
        report_year=record.report_year,
        retained_report_year=retained_year,
        retained_source_indices=matched_source_indices,
        jurisdiction=record.jurisdiction,
        permit_date=record.permit_date,
        tracking_id=record.tracking_id or record.weak_tracking_id,
        matched_source_indices=matched_source_indices,
        details=details,
    )


def _resolve_final_audit_lineage(
    records: Sequence[PermitRecord],
    active_indices: set[int],
    audits: Sequence[DedupAuditEntry],
) -> tuple[DedupAuditEntry, ...]:
    """Resolve every audit reference to rows retained after every rule pass."""

    records_by_source = {record.source_index: record for record in records}
    retained_sources = {records[index].source_index for index in active_indices}
    audits_by_source = {entry.source_index: entry for entry in audits}
    if len(audits_by_source) != len(audits):
        raise AssertionError("Every removed source row must have one audit entry")

    resolved_by_source: dict[int, tuple[int, ...]] = {}
    visiting: set[int] = set()

    def resolve(source_index: int) -> tuple[int, ...]:
        if source_index in retained_sources:
            return (source_index,)
        cached = resolved_by_source.get(source_index)
        if cached is not None:
            return cached
        if source_index in visiting:
            raise AssertionError("Deduplication audit lineage contains a cycle")
        entry = audits_by_source.get(source_index)
        if entry is None:
            raise AssertionError(
                "Deduplication audit references a row that was neither retained nor audited"
            )
        if not entry.matched_source_indices:
            raise AssertionError("Deduplication audit entry has no matched source row")

        visiting.add(source_index)
        terminals: set[int] = set()
        for matched_source in entry.matched_source_indices:
            if matched_source not in records_by_source:
                raise AssertionError(
                    "Deduplication audit references a source row outside the selected input"
                )
            terminals.update(resolve(matched_source))
        visiting.remove(source_index)
        if not terminals:
            raise AssertionError(
                "Deduplication audit lineage has no retained terminal row"
            )
        result = tuple(sorted(terminals))
        resolved_by_source[source_index] = result
        return result

    resolved_audits: list[DedupAuditEntry] = []
    for entry in audits:
        retained_source_indices = resolve(entry.source_index)
        retained_years = {
            records_by_source[source_index].report_year
            for source_index in retained_source_indices
        }
        if len(retained_years) != 1:
            raise AssertionError(
                "Deduplication audit lineage terminates in more than one report year"
            )
        retained_year = next(iter(retained_years))
        if retained_year <= entry.report_year:
            raise AssertionError(
                "Deduplication audit lineage does not terminate in a later report year"
            )
        if any(
            source_index not in retained_sources
            for source_index in retained_source_indices
        ):
            raise AssertionError(
                "Deduplication audit lineage contains a nonretained terminal row"
            )
        resolved_audits.append(
            replace(
                entry,
                retained_report_year=retained_year,
                retained_source_indices=retained_source_indices,
            )
        )
    return tuple(
        sorted(resolved_audits, key=lambda entry: (entry.source_index, entry.rule))
    )


def deduplicate_records(records: Iterable[PermitRecord]) -> DedupResult:
    """Apply the verified conservative and residual deduplication profile."""

    materialized = tuple(records)
    source_indices = [record.source_index for record in materialized]
    if len(source_indices) != len(set(source_indices)):
        raise ValueError("source_index must uniquely identify every record")
    active = set(range(len(materialized)))
    audits: list[DedupAuditEntry] = []

    def remove(
        index: int,
        *,
        rule: str,
        retained_year: int,
        retained_indices: Sequence[int],
        details: tuple[tuple[str, str], ...] = (),
    ) -> None:
        if index not in active:
            return
        active.remove(index)
        audits.append(
            _audit_entry(
                materialized[index],
                rule=rule,
                retained_year=retained_year,
                retained_indices=retained_indices,
                records=materialized,
                details=details,
            )
        )

    strong_blocks: dict[StrongBlockKey, list[int]] = defaultdict(list)
    id_dates: dict[IdKey, set[dt.date]] = defaultdict(set)
    weak_indices: list[int] = []
    for index, record in enumerate(materialized):
        if record.permit_date is None:
            continue
        if record.tracking_id:
            key = (record.jurisdiction, record.permit_date, record.tracking_id)
            strong_blocks[key].append(index)
            id_dates[(record.jurisdiction, record.tracking_id)].add(record.permit_date)
        else:
            weak_indices.append(index)

    strong_component_audits: dict[StrongBlockKey, _ComponentAudit] = {}
    for key, block_indices in strong_blocks.items():

        def edge(left_index: int, right_index: int) -> int | None:
            left = materialized[left_index]
            right = materialized[right_index]
            if left.report_year == right.report_year:
                return None
            return proposed_link_kind(
                left.addresses,
                right.addresses,
                left.raw_apn,
                right.raw_apn,
                left.formatted_apn,
                right.formatted_apn,
            )

        component_audit = _safe_components(block_indices, materialized, edge)
        strong_component_audits[key] = component_audit
        for component in component_audit.matched_components:
            latest_year = max(materialized[index].report_year for index in component)
            retained_indices = tuple(
                index
                for index in component
                if materialized[index].report_year == latest_year
            )
            for index in component:
                if materialized[index].report_year != latest_year:
                    remove(
                        index,
                        rule=RULE_STRONG_LINKED,
                        retained_year=latest_year,
                        retained_indices=retained_indices,
                        details=(("component_size", str(len(component))),),
                    )

    weak_fingerprint_blocks: dict[tuple[str, ...], list[int]] = defaultdict(list)
    for index in weak_indices:
        weak_fingerprint_blocks[materialized[index].raw_fingerprint].append(index)
    for block_indices in weak_fingerprint_blocks.values():
        by_year: dict[int, list[int]] = defaultdict(list)
        for index in block_indices:
            by_year[materialized[index].report_year].append(index)
        if len(by_year) < 2:
            continue
        latest_year = max(by_year)
        retained_indices = tuple(sorted(by_year[latest_year]))
        earlier_counts = [
            len(values) for year, values in by_year.items() if year != latest_year
        ]
        if len(retained_indices) < max(earlier_counts):
            continue
        for year, year_indices in by_year.items():
            if year == latest_year:
                continue
            for index in year_indices:
                remove(
                    index,
                    rule=RULE_WEAK_LITERAL,
                    retained_year=latest_year,
                    retained_indices=retained_indices,
                    details=(
                        (
                            "fingerprint_fields",
                            str(len(materialized[index].raw_fingerprint)),
                        ),
                    ),
                )

    collision_ids = _collision_ids(strong_blocks, materialized)
    unresolved_keys: set[StrongBlockKey] = set()
    split_keys: set[StrongBlockKey] = set()
    for key, block_indices in strong_blocks.items():
        if len({materialized[index].report_year for index in block_indices}) < 2:
            continue
        component_audit = strong_component_audits[key]
        if not component_audit.matched_components:
            unresolved_keys.add(key)
        if component_audit.is_split:
            split_keys.add(key)

    def strong_main_qualifies(key: StrongBlockKey) -> bool:
        jurisdiction, _permit_date, tracking_id = key
        indices = strong_blocks[key]
        if not (
            len(id_dates[(jurisdiction, tracking_id)]) == 1
            and (jurisdiction, tracking_id) not in collision_ids
            and _latest_count_guard(indices, materialized)
        ):
            return False
        latest_year = max(materialized[index].report_year for index in indices)
        latest_indices = [
            index
            for index in indices
            if index in active and materialized[index].report_year == latest_year
        ]
        return any(
            _strong_pair_evidence(materialized[index], materialized[latest_index])
            for index in indices
            if index in active and materialized[index].report_year != latest_year
            for latest_index in latest_indices
        )

    main_unresolved = {key for key in unresolved_keys if strong_main_qualifies(key)}
    main_split = {key for key in split_keys if strong_main_qualifies(key)}

    def remove_main_residual_earlier(key: StrongBlockKey, rule: str) -> None:
        block_indices = strong_blocks[key]
        latest_year = max(materialized[index].report_year for index in block_indices)
        latest_indices = tuple(
            index
            for index in block_indices
            if index in active and materialized[index].report_year == latest_year
        )
        for index in block_indices:
            if index not in active or materialized[index].report_year == latest_year:
                continue
            matches = [
                (
                    latest_index,
                    _strong_pair_evidence(
                        materialized[index], materialized[latest_index]
                    ),
                )
                for latest_index in latest_indices
            ]
            matches = [match for match in matches if match[1]]
            if not matches:
                continue
            retained_index, evidence = min(
                matches,
                key=lambda match: (
                    -len(match[1]),
                    match[1],
                    materialized[match[0]].source_index,
                ),
            )
            remove(
                index,
                rule=rule,
                retained_year=latest_year,
                retained_indices=(retained_index,),
                details=(
                    ("residual_block_size", str(len(block_indices))),
                    ("corroboration", ",".join(evidence)),
                ),
            )

    def remove_residual_earlier(
        key: StrongBlockKey, rule: str, evidence: Sequence[str] = ()
    ) -> None:
        block_indices = strong_blocks[key]
        latest_year = max(materialized[index].report_year for index in block_indices)
        retained_indices = tuple(
            index
            for index in block_indices
            if index in active and materialized[index].report_year == latest_year
        )
        for index in block_indices:
            if index in active and materialized[index].report_year != latest_year:
                remove(
                    index,
                    rule=rule,
                    retained_year=latest_year,
                    retained_indices=retained_indices,
                    details=(("residual_block_size", str(len(block_indices))),)
                    + ((("corroboration", ",".join(evidence)),) if evidence else ()),
                )

    for key in sorted(main_unresolved):
        remove_main_residual_earlier(key, RULE_STRONG_RELIABLE)
    for key in sorted(main_split):
        remove_main_residual_earlier(key, RULE_STRONG_SPLIT)

    remaining_unresolved = unresolved_keys - main_unresolved
    aggregate_keys = {
        key
        for key in remaining_unresolved
        if _aggregate_exception_qualifies(
            key, strong_blocks[key], id_dates, materialized
        )
    }
    reused_keys = {
        key
        for key in remaining_unresolved - aggregate_keys
        if _reused_id_exception_qualifies(
            key, strong_blocks[key], id_dates, materialized
        )
    }
    for key in sorted(aggregate_keys):
        remove_residual_earlier(
            key,
            RULE_STRONG_AGGREGATE,
            ("category_aggregate", "single_permit_date", "street_family"),
        )
    for key in sorted(reused_keys):
        remove_residual_earlier(
            key,
            RULE_STRONG_REUSED,
            ("reused_tracking_id", "exact_valid_apn", "exact_valid_coordinates"),
        )

    weak_remaining = [index for index in weak_indices if index in active]
    project_removals = _weak_candidate_removals(
        weak_remaining,
        materialized,
        require_substantive_project=True,
        require_dual_location=False,
    )
    dual_removals = _weak_candidate_removals(
        weak_remaining,
        materialized,
        require_substantive_project=False,
        require_dual_location=True,
    )
    for index in sorted(set(project_removals) | set(dual_removals)):
        project = project_removals.get(index)
        dual = dual_removals.get(index)
        if project and dual:
            rule = RULE_WEAK_PROJECT_AND_DUAL
            candidate = project
            retained_indices = tuple(
                sorted(set(project.retained_indices) | set(dual.retained_indices))
            )
            evidence = "project_and_dual_location"
        elif project:
            rule = RULE_WEAK_PROJECT
            candidate = project
            retained_indices = project.retained_indices
            evidence = project.evidence
        else:
            rule = RULE_WEAK_DUAL_LOCATION
            assert dual is not None
            candidate = dual
            retained_indices = dual.retained_indices
            evidence = dual.evidence
        remove(
            index,
            rule=rule,
            retained_year=candidate.retained_year,
            retained_indices=retained_indices,
            details=(("evidence", evidence),),
        )

    transformed_blocks: dict[tuple[object, ...], list[int]] = defaultdict(list)
    for index in weak_indices:
        if index not in active:
            continue
        record = materialized[index]
        if (
            record.permit_date is None
            or not record.project_key
            or record.project_key in GENERIC_PROJECTS
        ):
            continue
        transformed_blocks[
            (
                record.jurisdiction,
                record.permit_date,
                record.project_key,
                record.categories,
                record.reported_total,
                record.unit_category,
                record.tenure,
            )
        ].append(index)

    for block_indices in transformed_blocks.values():
        by_year: dict[int, list[int]] = defaultdict(list)
        for index in block_indices:
            by_year[materialized[index].report_year].append(index)
        if len(by_year) < 2 or any(
            len(year_indices) != 1 for year_indices in by_year.values()
        ):
            continue
        latest_year = max(by_year)
        latest_index = by_year[latest_year][0]
        for year in sorted(by_year):
            if year == latest_year:
                continue
            earlier_index = by_year[year][0]
            if house_number_moved_from_apn_to_address(
                materialized[earlier_index], materialized[latest_index]
            ):
                remove(
                    earlier_index,
                    rule=RULE_TRANSFORMED_LOCATION,
                    retained_year=latest_year,
                    retained_indices=(latest_index,),
                    details=(("evidence", "house_number_moved_from_apn"),),
                )

    retained = tuple(
        record for index, record in enumerate(materialized) if index in active
    )
    removed = tuple(
        record for index, record in enumerate(materialized) if index not in active
    )
    if len(audits) != len(removed):
        raise AssertionError("Every removed row must have exactly one audit entry")
    resolved_audits = _resolve_final_audit_lineage(materialized, active, audits)
    return DedupResult(retained=retained, removed=removed, audit=resolved_audits)


def deduplicate(records: Iterable[PermitRecord]) -> DedupeResult:
    """Public alias for the record API used by ingestion pipelines."""

    return deduplicate_records(records)


def deduplicate_rows(
    rows: Iterable[Mapping[str, object]],
    *,
    source_index_start: int = 2,
    columns: HcdColumns = DEFAULT_COLUMNS,
    fingerprint_fields: Sequence[str] | None = None,
    csv_physical_lines: Sequence[int | None] | None = None,
) -> DedupResult:
    """Build records from CSV mappings and apply :func:`deduplicate_records`."""

    return deduplicate_records(
        records_from_rows(
            rows,
            source_index_start=source_index_start,
            columns=columns,
            fingerprint_fields=fingerprint_fields,
            csv_physical_lines=csv_physical_lines,
        )
    )
