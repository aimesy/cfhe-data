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
- `data/processed/airtable_totals.json`, the cycle-aware input for Airtable, with 522 sixth-cycle and 17 seventh-cycle jurisdictions
- `data/processed/source_manifest.json`, with source URLs, resource identifiers, hashes, byte counts, catalog metadata, and DataStore checks
- `data/processed/audit_summary.json`, with selection totals, removal rules, and conservation checks
- `data/processed/airtable_audit_summary.json`, with the same checks and removal-rule breakdown applied to the mixed current-cycle scope
- `data/run/dedupe_decisions.jsonl`, a complete local decision ledger excluded from Git and GitHub Actions artifacts
- `data/run/dedupe_review.jsonl` and `data/run/airtable_dedupe_review.jsonl`, compact review ledgers containing every removal plus its directly matched and final referenced rows; these are attached to refresh workflow runs but never committed

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

The `Refresh HCD data` workflow runs daily and may be started manually. Unless a manual cutoff is supplied, it selects the most recent reporting year that has passed HCD's June 30 completeness date. It:

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

See [methodology](docs/methodology.md), [provenance](docs/provenance.md), and [operations](docs/operations.md).

## Webflow

`cfhe-data webflow-plan` can compare a reviewed totals file with a complete caller supplied CMS snapshot. It produces a digest bound plan containing only changed fields and their before and after values. It makes no network call and has no apply function.

An eventual Webflow apply workflow requires a protected GitHub Environment, a Webflow API token, site and collection identifiers, exact CMS field slugs, and a jurisdiction item mapping. Start from `config/webflow.example.json` after those values are available.

## Airtable publication

`cfhe-data airtable-sync` is the production GitHub-to-Airtable path. It fetches the complete base and schema, maps every source jurisdiction through its canonical key and the Airtable unincorporated flag, selects the declared sixth or seventh cycle row, and plans changes to only VLI, LI, MI, and AMI permit fields.

Without `--apply`, the command is read only. With `--apply`, it will proceed only when the source is complete, all 539 targets are unique and current, the schema still matches the pinned field IDs and types, and no Airtable progress override supersedes the permit fields. It rechecks values before each batch of ten, uses conditional updates, and verifies every record after writing. It never creates, deletes, upserts, relinks, or changes Airtable schema.

The GitHub job runs after CI on a push to `master` or an explicit automation dispatch bound to `master`, through the protected `airtable-production` environment. The PAT is limited to this base and is stored only as the `AIRTABLE_TOKEN` environment secret. See the [Airtable operations guide](docs/airtable.md).

The older `airtable-plan` command remains available for offline comparisons from supplied snapshots. Local snapshots and plans belong under ignored `data/airtable/` because they contain Airtable record IDs.

## Official sources

- [HCD APR dashboard and downloads](https://www.hcd.ca.gov/housing-open-data-tools/apr-dashboard)
- [California Open Data APR dataset](https://data.ca.gov/dataset/housing-element-annual-progress-report-apr-data-by-jurisdiction-and-year)
- Table A2 resource `fe505d9b-8c36-42ba-ba30-08bc4f34e022`
- Sixth cycle RHNA resource `1e80a9cf-724c-432d-8374-e9708a6a92dc`

HCD describes the records as jurisdiction reported data that it generally does not independently verify. HCD also states that the previous calendar year remains incomplete until June 30.

This data is a charitable gift to Yes In My Back Yard, a California nonprofit public benefit corporation recognized as exempt from federal income tax under Internal Revenue Code § 501(c)(3), EIN 32-0610451; to the extent I hold transferable rights in the data, its selection and arrangement, and accompanying materials, I irrevocably give, assign, and transfer all right, title, and interest in those rights without consideration.
