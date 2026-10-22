# cfhe-data

Reproducible HCD APR data pipeline for the California Fair Housing Elements tracker.

```text
HCD APR Table A2 + official planning periods
                      |
                      v
        source and schema validation
                      |
                      v
          audited snapshot deduplication
                      |
                      v
      sixth-cycle and current-cycle totals
                      |
                      v
       validated automation PR
                      |
                      v
       protected automatic Airtable update
```

## What it produces

- `data/processed/jurisdiction_totals.csv`, with one row per sixth cycle jurisdiction
- `data/processed/jurisdiction_totals.json`, the canonical input for a future Webflow update
- `data/processed/airtable_totals.json`, the input for Airtable, which declares each jurisdiction’s cycle and gives permit and entitlement counts for 522 sixth cycle and 17 seventh cycle jurisdictions
- `data/processed/source_manifest.json`, with source URLs, resource identifiers, hashes, byte counts, catalog metadata, and DataStore checks
- `data/processed/audit_summary.json`, with selection totals, removal rules, and conservation checks
- `data/processed/airtable_audit_summary.json`, with the same checks and removal-rule breakdown applied to the mixed current-cycle scope
- `data/processed/airtable_entitlement_audit_summary.json`, with the Table A2 entitlement columns used and the same selection totals, removal rules, and conservation checks applied to entitlements in the Airtable scope
- `data/processed/airtable_series.json`, the input for the APR Quarterly table in Airtable, with entitlements, building permits, and completions for each jurisdiction in every calendar quarter from 2018 through the cutoff year
- `data/run/dedupe_decisions.jsonl`, `data/run/airtable_dedupe_decisions.jsonl`, and `data/run/airtable_entitlement_decisions.jsonl`, complete local decision ledgers excluded from Git and GitHub Actions artifacts
- `data/run/dedupe_review.jsonl`, `data/run/airtable_dedupe_review.jsonl`, and `data/run/airtable_entitlement_review.jsonl`, compact review ledgers containing every removal plus its directly matched and final referenced rows; these are attached to refresh workflow runs but never committed

The CSV uses the requested labels `Undated Permits` and `Last Updated`. County names use `(Unincorporated)`.

## Local use

```powershell
python -m pip install -e ".[dev]"
cfhe-data refresh --cutoff-year 2025
python -m pytest -q
```

To rebuild from source files that have already been downloaded:

```powershell
cfhe-data build `
  --table-a2 data/raw/table_a2.csv `
  --rhna data/raw/rhna_progress_6.csv `
  --manifest data/processed/source_manifest.json `
  --cutoff-year 2025
```

## Refresh policy

The `Refresh HCD data` workflow runs daily, after relevant pipeline or configuration changes reach `master`, and on manual request. Unless a manual cutoff is supplied, it selects the most recent reporting year that has passed HCD's June 30 completeness date. It:

1. verifies the pinned CKAN package and resource identities;
2. downloads the complete source files and checks the catalog MD5 for uploaded CSV bytes;
3. checks the exact ordered CSV headers and the DataStore record totals;
4. applies the audited deduplication profile;
5. runs the test suite;
6. keeps raw source records and the complete decision ledgers out of downloadable artifacts;
7. attaches smaller review ledgers with each removal and the rows needed to inspect its direct and final references; and
8. creates or refreshes one automation pull request that links that evidence when the derived data changed;
9. explicitly dispatches CI against the exact refresh commit;
10. merges only after the required check succeeds; and
11. explicitly dispatches protected Airtable publication from the resulting `master` commit, or reconciles the existing `master` revision when HCD has not changed.

A failed build, validation, or merge leaves the refresh unmerged. A publication failure stops the current run and is retried against the same reviewed `master` revision the next day. Either kind of failure opens or updates one GitHub issue, and a later successful run closes it. A small monthly heartbeat commit prevents GitHub from disabling the schedule during long periods without HCD changes. GitHub never writes to Webflow.

## Deduplication boundary

The source contains annual snapshots. For dated rows with a substantive jurisdiction tracking identifier, the pipeline blocks on jurisdiction, permit date, and tracking identifier. It links records from different APR years only when address or APN evidence passes the audited compatibility rules. It keeps every row in the latest APR year of each linked component.

Rows with weak identifiers require an exact raw fingerprint, except for `YEAR`, or one of the separately audited residual predicates. Same year multiplicity is always preserved. Ambiguous rows remain. The HCD dashboard total is not a target.

Every stage then takes one more pass. It links rows from different APR years by date, income bands, count, and a shared APN, a compatible address, or a repeated report of another stage, ignores the tracking identifier except to keep sibling permits on one parcel apart, and keeps the latest APR year’s rows. See [methodology](docs/methodology.md).

See [methodology](docs/methodology.md), [provenance](docs/provenance.md), and [operations](docs/operations.md).

## Planning-period pace

The permit counter uses each jurisdiction’s HCD planning-period start and end. Config version 2 of the Airtable publisher also maintains `APR Data Through`, `APR Planning Period Start`, and `APR Planning Period End`. The reporting cutoff is December 31 of the source year, not the refresh date. Pace is RHNA multiplied by the elapsed fraction of that planning period, capped at the period boundaries. Equality qualifies as on target.

`cfhe-data pace-review --schema <schema.json> --snapshot <snapshot.json>` prepares a complete offline comparison with unchanged counts, proposed metadata updates, and exact formula replacements. Pace counts and formulas use permits only; entitlement fields in the totals are validated when present, and their metadata and the artifact digest carry through to the review. Snapshots and plans contain record IDs and belong under ignored `data/airtable/`. See [the pace correction](docs/pace-correction.md).

## Table B counter review

The production artifacts above still count Table A2 permits from the start of the planning period. They do not yet include all credit for the projection period. A separate `table-b-review` command checks submitted Table B sources against those artifacts and calculates pace at the APR reporting cutoff. It never writes to Airtable or substitutes missing reports with zero. It compares permits only and accepts artifacts with or without entitlement fields.

The initial source manifest covers Los Angeles, Petaluma, and South San Francisco. Source files must match the URLs and SHA-256 digests in `config/table_b_sources_2025.json` and remain under ignored `data/raw/`.

```powershell
cfhe-data table-b-review --manifest config/table_b_sources_2025.json
```

The command writes `data/run/table_b_review.json` and returns a nonzero status while coverage is incomplete, source validation fails, or differences beyond projection-period credit need reconciliation. City reports can omit records present in newer statewide data, so this command does not generate replacement permit values. See [the Table B review](docs/table-b-review.md).

## Webflow

`cfhe-data webflow-plan` can compare a reviewed totals file with a complete caller supplied CMS snapshot. It produces a digest bound plan containing only changed fields and their before and after values. It makes no network call and has no apply function.

An eventual Webflow apply workflow requires a protected GitHub Environment, a Webflow API token, site and collection identifiers, exact CMS field slugs, and a jurisdiction item mapping. Start from `config/webflow.example.json` after those values are available.

## Airtable publication

`cfhe-data airtable-sync` is the production path from GitHub to Airtable. It fetches the complete base and schema, maps every source jurisdiction through its canonical key and the Airtable unincorporated flag, and selects the declared sixth or seventh cycle row. Config version 1 manages VLI, LI, MI, and AMI permits. Version 2 additionally manages the APR reporting cutoff and planning dates. Version 3 additionally overwrites `🔍 Total VLI Entitlements`, `🔍 Total LI Entitlements`, `🔍 Total MI Entitlements`, and `🔍 Total AMI Entitlements` with Table A2 entitlement counts. Entitlements are not RHNA progress, are never added to permits, and never feed Total Progress or pace.

Without `--apply`, the command is read only. With `--apply`, it will proceed only when the source is complete, all 539 targets are unique and current, the schema still matches the pinned field IDs and types, and no Airtable progress override supersedes the permit fields. A blocker holds back every field in the plan, entitlements included. It rechecks values before each batch of ten, uses conditional updates, and verifies every record after writing. It never creates, deletes, upserts, relinks, or changes Airtable schema.

The GitHub job runs after CI on a push to `master` or an explicit automation dispatch bound to `master`, through the protected `airtable-production` environment. The PAT is limited to this base and is stored only as the `AIRTABLE_TOKEN` environment secret. See the [Airtable operations guide](docs/airtable.md).

The older `airtable-plan` command remains available for offline comparisons from supplied snapshots. Local snapshots and plans belong under ignored `data/airtable/` because they contain Airtable record IDs.

## Housing element compliance

`cfhe-data he-compliance-sync` copies HCD's weekly Housing Element Compliance Report into `HE Compliance` and `Review Date` on each current RHNA & HE record, which drives the public Builder's Remedy view. It runs daily after permit publication. Without `--apply` it is read only. See the [Airtable operations guide](docs/airtable.md#housing-element-compliance).

## Quarterly series

`cfhe-data series-sync` keeps the `APR Quarterly` table equal to `airtable_series.json`, so activity can be compared before and after a housing law or program takes effect. Each record is one jurisdiction in one calendar quarter and links to its Jurisdictions record. Each stage is dated by its own Table A2 column:

- entitlements by `ENT_APPROVE_DT1`;
- building permits by `BP_ISSUE_DT1`;
- completions by `CO_ISSUE_DT1`.

Every stage reports the income bands and the total, and splits units by type and by approval program. A quarter with no activity reports zeros. `Data Flags` warn when a year is incomplete or its dates are unreliable.

Entitlements and permits are deduplicated as they are for the cycle totals, and completions follow the permit rules. Before publication, the build checks that each jurisdiction’s quarters inside its planning period add up to the published entitlement and permit totals, and any difference blocks the sync. Without `--apply` the command is read only. The `publish-airtable` job runs it after the compliance sync. It updates and creates records but never deletes one. See [methodology](docs/methodology.md#quarterly-series) and the [Airtable operations guide](docs/airtable.md#quarterly-series).

## Official sources

- [HCD APR dashboard and downloads](https://www.hcd.ca.gov/housing-open-data-tools/apr-dashboard)
- [California Open Data APR dataset](https://data.ca.gov/dataset/housing-element-annual-progress-report-apr-data-by-jurisdiction-and-year)
- Table A2 resource `fe505d9b-8c36-42ba-ba30-08bc4f34e022`
- Sixth cycle RHNA resource `1e80a9cf-724c-432d-8374-e9708a6a92dc`
- [Housing Element Compliance Report](https://data.ca.gov/dataset/housing-element-compliance-report) resource `2dcd1cd4-1348-4fc5-9c9c-219f82daac00`

HCD describes the records as jurisdiction reported data that it generally does not independently verify. HCD also states that the previous calendar year remains incomplete until June 30.

This data is a charitable gift to Yes In My Back Yard, a California nonprofit public benefit corporation recognized as exempt from federal income tax under Internal Revenue Code § 501(c)(3), EIN 32-0610451; to the extent I hold transferable rights in the data, its selection and arrangement, and accompanying materials, I irrevocably give, assign, and transfer all right, title, and interest in those rights without consideration.
