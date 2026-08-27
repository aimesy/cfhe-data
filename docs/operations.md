# Operations

## Normal refresh

The scheduled workflow runs daily and may also be started manually. A merge to `master` that changes the pipeline, configuration, tests, or either workflow also starts it immediately. It downloads complete HCD snapshots, validates them, rebuilds both the sixth-cycle comparison totals and mixed current-cycle Airtable totals, and runs the tests. Raw source records and the complete decision ledgers remain on the temporary runner. The workflow uploads compact review ledgers containing every removal plus its directly matched and final referenced rows, along with both audit summaries, for 90 days. A refresh pull request links the workflow run that contains them.

The uploaded CSV is preferred. If that endpoint is unavailable, the workflow may use the official DataStore dump. The fallback is accepted only when its synthetic `_id` field is the sole extra column, its remaining ordered fields match the pinned source header, and its logical record count matches the DataStore total.

If the source digest and derived output have not changed, the run removes any obsolete automation pull request and reconciles the existing `master` revision with Airtable. If they have changed, the workflow updates the single `automation/hcd-refresh` branch and its pull request. It explicitly dispatches CI against the exact refresh commit, waits for the required `test` check, and merges only after that check succeeds. Because merges performed with GitHub's workflow token do not start another workflow implicitly, the refresh then explicitly dispatches CI on the resulting `master` commit with protected Airtable publication enabled. Nothing publishes to Webflow automatically.

The workflow updates `data/processed/refresh_heartbeat.json` once per calendar month. That creates repository activity even if HCD publishes no changes for an extended period, preventing GitHub from disabling the scheduled workflow for inactivity without creating daily heartbeat commits.

Any source, schema, coverage, cycle, aggregate, deduplication, test, merge, or Airtable precondition failure stops the chain. Validation failures are not merged. If Airtable publication fails after a validated merge, the daily reconciliation safely retries the same reviewed `master` revision. The workflow opens or updates one issue titled `Automated HCD refresh needs attention` and links the failed run. A successful later run closes the alert. It never bypasses branch protection or a failed required check.

Local equivalent:

```powershell
python -m pip install -e ".[dev]"
cfhe-data refresh --cutoff-year 2025
python -m pytest -q
```

## Failure review checklist

When the automation opens an alert instead of merging:

1. Confirm that both resource identifiers are unchanged.
2. Inspect the source byte lengths, SHA-256 digests, and HTTP dates.
3. Review schema changes before accepting them.
4. Review removals by rule and any unresolved candidates.
5. Compare jurisdiction changes, especially unusually large changes.
6. Confirm that the selected cutoff year is intentional.
7. Confirm that the selected year is no longer provisional before presenting it as complete. HCD states that the previous calendar year is not complete until June 30.
8. Review the current-cycle split and any change to `config/airtable_cycles.json` separately from permit changes.
9. Download the deduplication review artifact from the linked workflow run. Confirm that each removal includes its rule evidence, direct match, final retained reference, and matching retained report year.

## Webflow boundary

GitHub is the calculation and review layer. Webflow remains the presentation layer. The intended publication path is:

```text
HCD source -> GitHub refresh -> reviewed data pull request -> Webflow change plan -> approved Webflow update
```

The repository does not yet contain an apply workflow. Building that step requires:

- a Webflow API token stored as a GitHub Environment secret;
- the current site and collection identifiers;
- a jurisdiction item mapping;
- the exact CMS field slugs;
- an approval rule for the protected environment.

The first Webflow implementation should create a change plan and compare current CMS values before any write. An apply run should require the exact source digest, Git commit, and plan digest, and should update mapped items only. It should not create, delete, or publish CMS items automatically.

## Airtable publication boundary

GitHub owns the HCD calculation and review. Airtable remains the operational source for the tracker and its nonpermit fields. The publication job updates only the four permit bands on existing RHNA records.

The mixed-cycle artifact declares the target cycle and HCD counting period for each jurisdiction. Airtable cycle dates are audit context only and are not used as HCD counting windows. Canonical jurisdiction keys plus Airtable's unincorporated checkbox provide the exact mapping. Display spelling is not used as a fuzzy key.

The `airtable-production` GitHub Environment holds one secret, `AIRTABLE_TOKEN`. The token is restricted to the CFHE base and has only `data.records:read`, `data.records:write`, and `schema.bases:read`. No token belongs in the repository, an Actions log, or a saved plan.

The publication job checks the exact Git commit, full schema, terminal pagination, unique jurisdiction coverage, cycle selection, current formula, Correct Link formula, progress overrides, source and snapshot digests, and field prevalues. Updates are sent in batches of no more than ten. Every batch is read immediately before and after the write. A safe rerun recognizes values already written by an interrupted prior run.

The job never creates, deletes, upserts, relinks, or changes schema. A blocker or mismatch fails the job before the affected batch is sent.

## Recovery

Every source file is named by its content digest. A failed build leaves the prior committed output unchanged. Use the workflow logs and the official source configuration to reproduce the error locally. GitHub Actions deliberately does not retain raw snapshots or the complete decision ledgers. It retains only the smaller review evidence described above. Do not replace a prior source snapshot with a file that has different bytes under the same name.
