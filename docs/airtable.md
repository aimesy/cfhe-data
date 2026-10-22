# Airtable operations

GitHub is the calculation and review layer. Airtable is the operational target for the public tracker. The automated path is:

```text
HCD sources
    |
    v
validated and deduplicated current-cycle totals
    |
    v
validated automation pull request merged to master
    |
    v
protected GitHub Actions job
    |
    v
existing Airtable permit fields, APR reporting/planning dates, and entitlement fields
```

## Data ownership

HCD data processed in GitHub controls the permit bands:

- Airtable VLI = acutely low + extremely low + very low
- Airtable LI = low
- Airtable MI = moderate
- Airtable AMI = above moderate

Undated permits are already included in those bands and are never added again. Config version 2 also manages `APR Data Through`, `APR Planning Period Start`, and `APR Planning Period End`. These are date fields tied to the same source artifact: December 31 of `cutoff_year`, `period_start`, and `period_end`. The publication code leaves existing RHNA and HE date lookups unchanged. Config version 1 retains the original permit-only behavior.

Config version 3 manages everything version 2 manages and adds the entitlement fields below, from the Table A2 entitlement columns, counted over the same period and deduplicated by the permit rules plus one pass for entitlements that a later APR carried forward under a new tracking ID (see [methodology](methodology.md#entitlements)):

- `🔍 Total VLI Entitlements` = acutely low + extremely low + very low
- `🔍 Total LI Entitlements` = low
- `🔍 Total MI Entitlements` = moderate
- `🔍 Total AMI Entitlements` = above moderate

Entitlements are not RHNA progress. They must never be added to permits, and they never feed Total Progress, the pace formulas, the progress override check, or any other RHNA progress figure. The sync overwrites all four fields from the reviewed source, so a hand edit to a blank or a nonnegative whole number is replaced on the next run. A negative, fractional, or nonnumeric value in any of these fields, or in a permit field, on any RHNA row stops publication before any write, and someone must correct it by hand. A jurisdiction with no retained entitlement rows receives 0 in each field, not a blank. Undated entitlements are already included in the bands.

The publication artifact is `data/processed/airtable_totals.json`. It declares one target cycle and one HCD counting period per jurisdiction. The default is the official sixth-cycle period. `config/airtable_cycles.json` replaces that period for the 17 jurisdictions now in the seventh cycle. Airtable dates are not used as counting windows.

## Identity and scope

The source key is the normalized HCD `jurisdiction_key`. Airtable jurisdiction names are normalized only for case and the explicit `(Unincorporated)` or `(Unincorporated Areas)` suffix. The Airtable unincorporated checkbox must also agree. There is no fuzzy matching.

Each of the 539 source jurisdictions must map to exactly one Airtable jurisdiction record and exactly one RHNA record for its declared cycle. `Sitekick City` is the sole explicit exclusion. An added, missing, or duplicate jurisdiction stops publication.

The exact base, table, and field IDs are pinned in `config/airtable_sync.json`. A schema type change stops publication before any record write.

## Credential

Create one Airtable personal access token named `cfhe-data GitHub Actions` with:

- resource access limited to the CFHE Airtable base;
- `data.records:read`;
- `data.records:write`; and
- `schema.bases:read`.

Do not grant schema write, comment, webhook, workspace, or user scopes.

Store the token only as the `AIRTABLE_TOKEN` secret in the GitHub Environment named `airtable-production`. A local token may be read from the protected Amybot `.sensitive` area and placed in the process environment temporarily. Never save it in this repository, a plan, or a command line argument.

## Local dry run

Set `AIRTABLE_TOKEN` in the process environment, then run:

```powershell
cfhe-data airtable-sync `
  --totals data/processed/airtable_totals.json `
  --config config/airtable_sync.json `
  --git-sha (git rev-parse HEAD)
```

This fetches the complete schema and both tables, builds a plan, and prints only counts and digests. It sends no record write. If a detailed plan is needed for local review, add `--plan-output data/airtable/change-plan.json`. The directory is ignored because the plan contains Airtable record IDs.

## Apply behavior

The GitHub job adds `--apply`. The plan is eligible only when:

- the source reporting year is complete;
- all source, cycle, and aggregate invariants reconcile;
- all 539 target records are unique;
- every target is marked current and passes Correct Link;
- no target has a nonzero progress override;
- the schema IDs and types match;
- Airtable has not changed since the plan was built; and
- the running Git commit is still the head of `master`.

A blocker holds back the whole plan. Under config version 3 that includes the entitlement fields, even though entitlements never affect progress.

Before each batch, the client reads every target record again. It sends at most ten updates to existing records and includes only changed permit fields, changed APR dates under config version 2 or 3, and changed entitlement fields under config version 3. It then reads every updated record back. A value that matches neither the reviewed old value nor the desired new value stops the batch. A rerun after an interrupted request safely accepts records that already contain the desired values.

After all batches, the command fetches the entire snapshot again and requires zero remaining changes and exact statewide aggregates.

Only after that complete readback succeeds, the command writes the same UTC timestamp to `Last APR Verified` for all 539 matched current records. This field therefore means the jurisdiction was checked against the reviewed HCD source, even when its permit figures were already correct. Under config version 3 it also certifies the four entitlement fields. `Last APR Update` remains the separate Airtable timestamp for an actual target or permit value change.

The client has no create, delete, upsert, relink, or schema mutation method. Creating the date and entitlement fields and migrating the pace formulas are separate one-time setup. The ordinary automation still needs no permission to write schema. See [pace correction](pace-correction.md) for the formula behavior and before/after review.

## Config version 3 rollout

The four entitlement fields were created by hand in the RHNA & HE table as number fields with precision 0. Version 3 pins them the way it pins the permit fields. Read the schema, add each field ID to `rhna_fields` under `entitlement_vli`, `entitlement_li`, `entitlement_mi`, and `entitlement_ami`, add each field's option digest to `rhna_field_options_sha256` under the same names, and set `config_version` to 3. A number field with precision 0 has the same option digest as the permit bands. A missing ID or digest stops the config from loading, and a later change of field type or precision stops publication.

Version 3 requires the entitlement fields in `airtable_totals.json` and validates them: every value is a nonnegative integer, the six categories sum to `entitlement_total` on each row, and the metadata counts reconcile with the rows. Versions 1 and 2 still reject an artifact that carries those fields, and otherwise build the same plans, digests, and summaries as before. CI publishes on every push to `master`, and a pipeline change that reaches `master` starts a refresh that regenerates the artifact. The pipeline change, the regenerated artifact, and the version 3 config must therefore reach `master` in the same merge. `tests/test_airtable_sync.py` runs the committed config against the committed artifact, and the refresh runs the tests on the artifact it has just built, so a pair that disagrees fails CI or the refresh before it can merge. If one reaches `master` anyway, publication stops before any record write.

Under version 3 the plan reports `airtable_entitlement_aggregate_before` and `source_entitlement_aggregate_after` separately from the permit aggregates, and the printed summary adds `source_entitlement_total` beside `source_total`. `pace-review` and `table-b-review` accept an artifact with or without the entitlement fields. They validate the fields when present and reject a partial set, but neither review uses them.

## GitHub controls

The publication job is part of CI and has these boundaries:

- it runs for a push to `master`, or for the refresh workflow's explicit dispatch bound to `master`;
- it waits for the test job;
- it checks out the exact push SHA and confirms that SHA is still `origin/master`;
- it uses `contents: read` permission;
- it uses the `airtable-production` Environment;
- concurrent publication runs queue instead of cancelling one another; and
- pull request jobs cannot access or invoke the token.

The daily refresh explicitly runs the publication command even when the reviewed HCD artifact has not changed. This makes publication idempotent and allows a transient Airtable or GitHub failure to recover on the next scheduled run.

The first live run and manual readback check were completed on August 27, 2026. The temporary reviewer requirement was removed. The Environment still permits deployment only from protected branches, and every publication still requires a successful `test` job and an exact current `master` check.

## Offline comparison

`cfhe-data airtable-plan` remains as a legacy offline comparison tool. It accepts a caller supplied snapshot and writes a digest bound local plan. It cannot make a network call or write to Airtable. Use `airtable-sync` for the production path.

## Housing element compliance

`cfhe-data he-compliance-sync` keeps `HE Compliance` and `Review Date` on each jurisdiction's current RHNA & HE record equal to HCD's [Housing Element Compliance Report](https://data.ca.gov/dataset/housing-element-compliance-report), which HCD updates weekly. The Builder's Remedy, SB 423, and Jurisdictions compliance fields read those two fields, so the public Builder's Remedy view on yimbylaw.org follows HCD. HCD certification is the standard YIMBY Law uses for that page.

HCD `In` maps to `In Compliance`. HCD `Out` and `Enforcement Out` map to `Out of Compliance`. The command resolves the pinned CKAN resource in `config/he_compliance_sync.json`, checks the exact CSV header, and matches names by the same rules as the permit sync, with accents folded so `San José` matches `SAN JOSE`.

Review Date drives the "Applied until" text in the Builder's Remedy formula. HCD's Reviewed Date is the latest review, which can be later than the first compliance finding, so the sync writes it only in the same update that changes a record's compliance status. Otherwise the existing Review Date stays.

A jurisdiction is held, and left unchanged, when it has no single current RHNA & HE record, when HCD reports a different cycle than the current Airtable record, or when HCD's status is blank. Holds are listed in the output. Publication is blocked when any Airtable jurisdiction is missing from HCD or the reverse, when the matched count differs from the configured count, when a pinned field type or option set changes, or when more than a quarter of jurisdictions would be out of compliance.

Apply rebuilds the plan from fresh Airtable and HCD reads and requires the same plan digest before writing. Updates are conditional on the reviewed old values and are read back, and a final full read must show no remaining changes. Volunteer edits to `HE Compliance` that disagree with HCD are overwritten on the next run.

The step runs in the `publish-airtable` job after the permit publication, so the daily refresh runs it. The first application, on October 19, 2026, corrected 95 records: 75 from out of compliance to in, 15 blank to in, 3 blank to out, and Brisbane and Tulare from in to out.

## Quarterly series

`cfhe-data series-sync` publishes `airtable_series.json` to the `APR Quarterly` table. `config/airtable_series_sync.json` pins the table, its field IDs and option digests, and the Jurisdictions name and unincorporated fields. A record is identified by its single Jurisdictions link together with `Year` and `Quarter`; `Key` is a label and plays no part in matching. Jurisdiction names match by the same rules as the permit sync. Unlike the compliance sync, the series sync does not fold accents.

The plan matches every series row to one record. It rewrites the changed fields of matched records and creates the rows still missing. Before creating, it fills records whose fields are all blank, such as the rows Airtable adds to a new table. It never deletes. A record with a valid identity outside the series is reported as an orphan and left alone. Publication is blocked when:

- the series build recorded a sync blocker;
- an Airtable jurisdiction has no series rows, or a series jurisdiction has no single Airtable match;
- a record is neither blank nor fully identified;
- two records share a jurisdiction, year, and quarter;
- a pinned field type or option set has changed.

Apply rebuilds the plan from a fresh read and requires the same plan digest before writing. Updates are conditional on the reviewed old values, and a final full read must show no remaining change. A record someone adds by hand during an apply can repeat a created row; the final read then reports the repeat, and publication stays blocked until it is resolved. Volunteer edits to series fields are overwritten on the next run. The series sync does not stamp `Last APR Verified`.

The step runs in the `publish-airtable` job after the compliance sync, even when an earlier step failed. Creation writes ten records per request, so the first apply takes about ten minutes. An update reads each changed record twice before writing it, at four requests a second, so a rewrite of the whole table takes about two and a half hours; the job allows 180 minutes.
