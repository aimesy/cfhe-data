# Methodology

## Source scope

The pipeline reads two official California Department of Housing and Community Development sources:

1. APR Table A2, which contains the project records and building permit fields.
2. The sixth cycle RHNA progress file, which supplies each jurisdiction’s planning period.

The HCD data is reported by jurisdictions. HCD states that it does not independently verify most of that information. A county jurisdiction row refers only to the county’s unincorporated territory.

The input CSV files are immutable within a run. Before parsing either file, the build recomputes its byte length, SHA-256 digest, and, when present, MD5 digest and compares them with the manifest. The manifest records the configured URL, final URL after redirects, resource identifier, byte length, HTTP metadata, retrieval time, and digests. Large source files and the detailed decision ledger are neither committed to Git nor uploaded as GitHub Actions artifacts.

## Selection

For every jurisdiction in the sixth cycle file, the pipeline includes a dated permit row only when `BP_ISSUE_DT1` falls between the jurisdiction’s planning period start and the selected cutoff. A row with a blank permit date is included by its APR reporting year when that year falls in the same interval. A nonblank date that cannot be parsed is not treated as a blank date.

The six output bands are calculated from the deed restricted and not deed restricted building permit columns. Acutely low, extremely low, and very low remain separate in the canonical output. They may be combined only by a downstream presentation that explicitly needs the older VLI grouping.

## Deduplication

The source is a sequence of annual snapshots. A project can therefore appear in several APR years even when the permit activity occurred once. The active rules remove only an earlier snapshot when the record linkage evidence is sufficient.

For a dated row with a substantive jurisdiction tracking identifier:

1. Block by normalized jurisdiction, permit date, and tracking identifier.
2. Compare rows from different APR years only.
3. Link addresses when the house and unit identifiers agree and the normalized street is exact or differs by at most one edit. Standard street suffixes and directions are normalized.
4. If address evidence is unavailable, allow a link when a valid normalized APN agrees.
5. Reject a bridge that would join incompatible locations.
6. Keep every row from the latest APR year in each linked component. This preserves all multiplicity within that year.

For a dated row with a blank or generic tracking identifier, the baseline rule requires every raw field except `YEAR` to match literally. It removes an earlier snapshot only when the latest snapshot has at least the same row multiplicity.

The residual rules require each earlier row to match at least one later snapshot using either an exact valid APN, exact valid coordinates, or at least two independent signals among substantive project metadata, street, and house number. A house number, street, or project name alone cannot remove a row. Populated but conflicting unit identifiers veto residual removal, including when APN or coordinates agree. The rules retain ambiguous cases, keep every latest APR row, and preserve every duplicate within one APR year. The audit ledger preserves the directly matched rows, resolves each chain to the rows retained after every rule pass, and records a retained report year that agrees with every final reference.

The dashboard total is not a target for these rules. A difference from the dashboard is reported, not optimized away.

## Entitlements

The Airtable build also totals completed entitlements from the same Table A2 rows. It reads the unprefixed income columns, not the `BP_` permit columns, and uses the permit band names. Acutely low adds `ACUTELY_LOW_INCOME_DR` and `ACUTELY_LOW_INCOME_NDR`; extremely low, very low, low, and moderate add the deed restricted and not deed restricted columns of `EXTREMELY_LOW_INCOME`, `VLOW_INCOME`, `LOW_INCOME`, and `MOD_INCOME` the same way; above moderate is `ABOVE_MOD_INCOME` alone. The bands are exclusive, and `EXTR_LOW_INCOME_UNITS` is never added to any of them.

Entitlement rows use the permit selection window. A dated row is included only when `ENT_APPROVE_DT1` falls between the jurisdiction’s HCD planning period start and the earlier of its period end and the selected cutoff. That period is the one the artifact declares, taken from the RHNA file or replaced by `config/airtable_cycles.json`; Airtable dates are never used. A row with a blank approval date and entitlement units is included by its APR reporting year when that year falls in the same interval. Permits and entitlements are selected separately in one pass over the file, so a row can contribute a permit, an entitlement, or both. On every selected row the bands must sum to `NO_ENTITLEMENTS`; a single mismatch stops the build, as a mismatch with `NO_BUILDING_PERMITS` does for permits.

Entitlement records then run through the deduplication rules above in their own pass, blocked on the approval date in place of the permit date. A project whose permit and entitlement were reported in different APR years is therefore resolved separately for each activity.

Entitlements then take one more pass that permits never take. A later APR sometimes repeats an earlier year’s entitlement with its original approval date under a new tracking identifier, so the rows fall in different blocks above and both survive. This pass links rows of one jurisdiction from different APR years that report the same approval date, the same income bands, and the same `NO_ENTITLEMENTS`, when the two rows share a valid APN or a compatible address; a row also joins through a chain of such links. When the entitlement totals at least five units, two such rows also link without a parcel or address match if both carry the same nonblank `BP_ISSUE_DT1` and the same positive `NO_BUILDING_PERMITS`, because a later APR that changes a project’s parcel or address still repeats its building permit. Project name, unit category, tenure, and unit identifiers are ignored, the tracking identifier matters only in the sibling check below, and a shared parcel links rows even when the address changed. Rows an earlier rule removed still count toward the linked component. The pass keeps every row from the latest APR year and removes the earlier rows only when that year has at least as many rows as each earlier year; otherwise it keeps the component whole. It also keeps an earlier row when removing it would leave any audit chain ending in more than one APR year. When the latest year holds more than one row, it keeps an earlier row whose only links to that year are a shared parcel with rows at other addresses whose tracking identifiers differ from its own but follow the same numbering pattern, as sibling permits on a subdivision’s master parcel do. The pass needs the exact approval date and income bands, so a later APR that moves the approval date or reallocates the bands of an earlier entitlement is not linked, and both reports remain in the totals. Its removals appear in the audit under `entitlement_carried_forward_snapshot`.

Each `airtable_totals.json` row adds an `entitlement_` field for every band, `entitlement_total`, `undated_entitlements`, and `entitlement_data_status`. A jurisdiction with no selected entitlement row reports zeros and `no_selected_rows`. The metadata adds the selected, retained, removed, and undated entitlement counts. `airtable_entitlement_audit_summary.json` records the source columns, selection statistics, category totals, removals by rule, and invariants. The complete entitlement decision ledger and its compact review ledger name each row’s date `entitlement_date`. The review ledger is uploaded with the run, the audit summary is committed with the other processed files, and the complete ledger stays on the runner.

The permit fields are rendered before the entitlement fields are merged. The build stops unless removing the entitlement fields reproduces that rendering byte for byte, so entitlements cannot change a permit total or `airtable_audit_summary.json`. The `jurisdiction_totals` build parses only the permit columns.

## Quality gates

A build stops on a missing required column, duplicate header, malformed row, malformed planning period, or invalid nonblank permit or entitlement approval date. A build checks each date it reads on every row of a jurisdiction with a planning period, before applying the window, so a bad date stops the build even on a row that is never counted. On a selected row, a negative, fractional, or nonnumeric permit or entitlement count also stops the build, as do bands that do not sum to `NO_BUILDING_PERMITS` or `NO_ENTITLEMENTS`. The audit also checks:

- input selection equals retained rows plus removed rows;
- every removed row resolves through an acyclic chain to at least one later retained snapshot;
- every final retained reference exists, remains in the output, and agrees with the declared retained report year;
- the newest APR multiplicity is preserved;
- aggregation happens only after deduplication;
- category totals reconcile with the jurisdiction rows;
- identical inputs produce identical outputs.

Live totals are not hard coded. A saved source hash may have a separate golden regression record, but that record runs only against the matching source bytes.
