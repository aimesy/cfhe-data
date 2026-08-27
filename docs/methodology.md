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

## Quality gates

A build stops on a missing required column, duplicate header, malformed row, negative or fractional permit count, malformed planning period, or invalid nonblank permit date. The audit also checks:

- input selection equals retained rows plus removed rows;
- every removed row resolves through an acyclic chain to at least one later retained snapshot;
- every final retained reference exists, remains in the output, and agrees with the declared retained report year;
- the newest APR multiplicity is preserved;
- aggregation happens only after deduplication;
- category totals reconcile with the jurisdiction rows;
- identical inputs produce identical outputs.

Live totals are not hard coded. A saved source hash may have a separate golden regression record, but that record runs only against the matching source bytes.
