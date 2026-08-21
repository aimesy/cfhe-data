# Operations

## Normal refresh

The scheduled workflow runs each Friday and may also be started manually. It downloads complete HCD snapshots, validates them, rebuilds the jurisdiction totals, and runs the tests. Raw source records and the detailed decision ledger remain on the temporary runner only and are not uploaded as artifacts.

The uploaded CSV is preferred. If that endpoint is unavailable, the workflow may use the official DataStore dump. The fallback is accepted only when its synthetic `_id` field is the sole extra column, its remaining ordered fields match the pinned source header, and its logical record count matches the DataStore total.

If the source digest and derived output have not changed, the run creates no commit. If they have changed, the workflow opens a draft pull request. A person reviews and merges that pull request. Nothing in this repository publishes to Webflow automatically.

Local equivalent:

```powershell
python -m pip install -e ".[dev]"
cfhe-data refresh --cutoff-year 2025
python -m pytest -q
```

## Review checklist

Before merging a refresh:

1. Confirm that both resource identifiers are unchanged.
2. Inspect the source byte lengths, SHA-256 digests, and HTTP dates.
3. Review schema changes before accepting them.
4. Review removals by rule and any unresolved candidates.
5. Compare jurisdiction changes, especially unusually large changes.
6. Confirm that the selected cutoff year is intentional.
7. Confirm that the current year is no longer provisional before presenting it as complete. HCD states that the previous calendar year is not complete until June 30.

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

## Recovery

Every source file is named by its content digest. A failed build leaves the prior committed output unchanged. Use the workflow logs and the official source configuration to reproduce the error locally. GitHub Actions deliberately does not retain raw snapshots or the detailed decision ledger. Do not replace a prior source snapshot with a file that has different bytes under the same name.
