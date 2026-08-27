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
four existing Airtable permit fields
```

## Data ownership

HCD data processed in GitHub controls only the four permit bands:

- Airtable VLI = acutely low + extremely low + very low
- Airtable LI = low
- Airtable MI = moderate
- Airtable AMI = above moderate

Undated permits are already included in those bands and are never added again. Airtable continues to control every other tracker field.

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

Before each batch, the client reads every target record again. It sends at most ten existing-record updates and includes only changed permit fields. It then reads every updated record back. A value that matches neither the reviewed old value nor the desired new value stops the batch. A rerun after an interrupted request safely accepts records that already contain the desired values.

After all batches, the command fetches the entire snapshot again and requires zero remaining changes and exact statewide aggregates.

The client has no create, delete, upsert, relink, or schema mutation method.

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
