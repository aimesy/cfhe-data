---
title: Planning-period pace correction
visibility: public
classification: project-documentation
---

# Planning-period pace correction

The counter already selects permits from HCD’s planning-period start through the earlier of the planning end and the APR reporting cutoff. The corrected benchmark uses the same dates. It compares the reported permit total with RHNA multiplied by elapsed planning days divided by total planning days. Equality counts as on target. The benchmark is a measure of progress, not a legal compliance determination.

## Verified effect

The September 19, 2026 review matched all 539 current Airtable totals against the September 18 source artifact. Holding those counts constant, correcting the clock alone changes 76 on target and 463 off target to 98 on target and 441 off target.

The regional-date audit also found that the download contains a superseded Siskiyou planning period. HCD’s current schedule identifies February 15, 2023 through February 15, 2031 for the county and its cities. Applying that window changes Etna from 1 permit to 0 and unincorporated Siskiyou County from 121 to 119. Their excluded permits were dated December 28, 2022 and February 1, 2023, respectively. No other jurisdiction’s permit total changes. The combined correction gives 502,080 permits statewide and 97 jurisdictions on target (442 off target), an increase of 21 on target. RHNA allocations remain unchanged.

The [regional correction provenance](../config/siskiyou_planning_period_correction.json) identifies the exact official schedule rows, response hash, and corroborating adopted Housing Element. The correction accepts the superseded or already-corrected source dates and rejects an unexpected future change for review. San Luis Obispo and Colusa dates retain December 31 boundaries, supported by HCD’s schedule and methodology.

| Example | Permits | Existing minimum for on target | Corrected minimum | Result |
|---|---:|---:|---:|---|
| Los Angeles | 89,753 | 279,502 | 240,355 | Still off target |
| Oakland | 2,703 | 13,101 | 9,568 | Still off target |
| South San Francisco | 1,485 | 1,975 | 1,442 | Changes to on target |

These existing thresholds describe the observed live formulas on September 19. The old formula advances with `TODAY()` even when the permit reporting cutoff is unchanged. The replacement moves only when the published APR cutoff changes. Bay Area pace becomes 36.4476% of RHNA; SCAG pace becomes 52.6352%.

## Managed dates and formulas

Config version 2 adds date fields named `APR Data Through`, `APR Planning Period Start`, and `APR Planning Period End` to the publisher’s explicit write allowlist. Every publication sets them from the same validated artifact as the permit totals. `Last APR Verified` remains a refresh timestamp. Existing HE and RHNA dates retain their separate roles.

The existing `% Time Remaining` and `On Target?` formula field IDs are preserved. For current rows, they use APR dates and an inclusive comparison. Integer cross multiplication avoids a rounded percentage deciding the status. Before the cycle starts, with missing dates, or with no positive allocation, pace is unavailable. At or after the planning end, reaching RHNA yields `Target Met`; otherwise the result is `Missed`. Historical rows keep their prior formulas.

Dependency review found that `% Time Remaining` feeds `On Target?`, which feeds the existing public `Current Progress` lookup and RHNA status colors. Income-band achievement percentages do not depend on the pace clock and remain unchanged.

## Verification and source limitations

The offline review rejects incomplete jurisdiction coverage, changed identities, cycle mismatches, allocation inconsistencies, progress overrides, and missing inputs. By default it requires unchanged permit totals. Supplying `--baseline-totals` permits a combined review of a new source against the exact before-change source; live counts must match that baseline, and every count difference is reported. Production publication also checks the pinned field types/options, expected values immediately before updates, and complete readback afterward. Dates and permit totals are written together in each guarded record patch.

This correction preserves the source artifact’s `reported` and `no_selected_rows` distinctions. It does not certify an absent source row as proof of zero permits. A separate Table B review found missing projection-period credits and conflicting city reports. Those findings do not authorize automatic replacement of newer A2 records with older summaries. See [Table B review](table-b-review.md).
