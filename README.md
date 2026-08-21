# cfhe-data

Private, reproducible HCD APR data pipeline for the California Fair Housing Elements tracker.

```text
HCD APR Table A2 + sixth cycle planning periods
                      |
                      v
        source and schema validation
                      |
                      v
          audited snapshot deduplication
                      |
                      v
        jurisdiction CSV and JSON totals
                      |
                      v
              reviewed GitHub PR
                      |
                      v
        Webflow change plan, then approval
```

The repository is the calculation and review layer. It does not publish to Webflow.

## What it produces

- `data/processed/jurisdiction_totals.csv`, with one row per sixth cycle jurisdiction
- `data/processed/jurisdiction_totals.json`, the canonical input for a future Webflow update
- `data/processed/source_manifest.json`, with source URLs, resource identifiers, hashes, byte counts, catalog metadata, and DataStore checks
- `data/processed/audit_summary.json`, with selection totals, removal rules, and conservation checks
- `data/run/dedupe_decisions.jsonl`, a complete decision ledger preserved as a private workflow artifact

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

The `Refresh HCD data` workflow runs weekly and may be started manually. It:

1. verifies the pinned CKAN package and resource identities;
2. downloads the complete source files and checks the catalog MD5 for uploaded CSV bytes;
3. checks the exact ordered CSV headers and the DataStore record totals;
4. applies the audited deduplication profile;
5. runs the test suite;
6. preserves the complete sources and decision ledger as private workflow artifacts; and
7. opens a draft pull request when the derived data changed.

It never merges a pull request or writes to Webflow.

## Deduplication boundary

The source contains annual snapshots. For dated rows with a substantive jurisdiction tracking identifier, the pipeline blocks on jurisdiction, permit date, and tracking identifier. It links records from different APR years only when address or APN evidence passes the audited compatibility rules. It keeps every row in the latest APR year of each linked component.

Rows with weak identifiers require an exact raw fingerprint, except for `YEAR`, or one of the separately audited residual predicates. Same year multiplicity is always preserved. Ambiguous rows remain. The HCD dashboard total is not a target.

See [methodology](docs/methodology.md), [provenance](docs/provenance.md), and [operations](docs/operations.md).

## Webflow

`cfhe-data webflow-plan` can compare a reviewed totals file with a complete caller supplied CMS snapshot. It produces a digest bound plan containing only changed fields and their before and after values. It makes no network call and has no apply function.

An eventual Webflow apply workflow requires a protected GitHub Environment, a Webflow API token, site and collection identifiers, exact CMS field slugs, and a jurisdiction item mapping. Start from `config/webflow.example.json` after those values are available.

## Official sources

- [HCD APR dashboard and downloads](https://www.hcd.ca.gov/housing-open-data-tools/apr-dashboard)
- [California Open Data APR dataset](https://data.ca.gov/dataset/housing-element-annual-progress-report-apr-data-by-jurisdiction-and-year)
- Table A2 resource `fe505d9b-8c36-42ba-ba30-08bc4f34e022`
- Sixth cycle RHNA resource `1e80a9cf-724c-432d-8374-e9708a6a92dc`

HCD describes the records as jurisdiction reported data that it generally does not independently verify. HCD also states that the previous calendar year remains incomplete until June 30.
