---
title: RHNA counter source review
visibility: public
classification: project-documentation
---

# RHNA counter source review

The intended counter compares current-cycle RHNA progress with the fraction of the Housing Element planning period elapsed at the same reporting cutoff. A 600-unit allocation requires 300 units at the halfway point under this linear benchmark. Equality qualifies as on pace. This benchmark does not itself determine statutory compliance.

## Implementation status

`progress.calculate_progress` implements the benchmark using actual planning dates, a supplied reporting cutoff, and exact arithmetic for the minimum whole-unit count. It handles five- and eight-year periods, caps elapsed time at the cycle boundaries, and leaves zero-allocation or unstarted-cycle judgments unavailable.

`table_b.read_table_b_workbook` reads submitted 2025 HCD workbooks without executing macros or altering the source. It verifies the jurisdiction, reporting year, cycle, layout, cached values, income-band sums, annual sums, projection credit, and allocation totals. Very-low-income credit already includes the acutely and extremely low subcategories for the supported cycles, so those subtotals are not added again. Other layouts and separate acutely/extremely low allocations require explicit support.

`cfhe-data table-b-review` checks the complete expected jurisdiction list against a source manifest and produces a comparison report. PDF sources use explicitly reviewed transcriptions with the complete source file, page, URL, and digest. Missing reports are listed without generating replacement values. Duplicate cumulative sources are rejected. Differences beyond the projection credit are exposed for reconciliation.

The permit selection policy remains the planning-period window. The pace correction uses that same period and the APR reporting cutoff. Table B review is separate: complete source coverage and reconciliation are required before changing the count definition or replacing permit totals.

## Initial comparisons

Compared with repository commit `f5db46d11112382765f71b77fffcd21d7025b8cf`, covering permits through December 2025:

| Locality | Existing count | Published Table B | Difference | Projection credit included | Difference beyond projection credit |
|---|---:|---:|---:|---:|---:|
| Los Angeles | 89,753 | 86,573 | -3,180 | 5,984 | -9,164 |
| Petaluma | 365 | 652 | +287 | 287 | 0 |
| South San Francisco | 1,485 | 770 | -715 | 28 | -743 |

These are comparisons, not approved replacement figures. Petaluma’s difference is explained by the projection column. The September 18 statewide source includes 742 South San Francisco units absent from the published PDF (480 at PS Office Park and 262 at 1400/1440 Huntington Avenue). The PDF also differs by one unit elsewhere and has changed income classifications. Replacing 1,485 with 770 would discard newer records.

Los Angeles has both duplicated source records and conflicting historical prefill values. Its 2025 Table B also includes 80 moderate-income conversion credits from Table F2. The 9,164-unit residual cannot be treated as one correction. Arithmetic agreement inside a workbook does not resolve these source conflicts. The review checks residuals separately in every income band so offsetting errors cannot disappear in the total.

Sources:

- [Los Angeles revised 2025 workbook](https://planning.lacity.gov/odocument/49d91c5e-4825-43dc-a267-3918c1efa3df/26_06_11_LosAngeles2025_TabC_TempData.xlsm), Table B cells N17, N19, N21, N23, N25 and D25.
- [Petaluma revised 2025 report](https://storage.googleapis.com/proudcity/petalumaca/2026/05/e1f6a290-2025-housing-element-apr-updated-05.04.pdf), PDF page 7.
- [South San Francisco 2025 report](https://ci-ssf-ca.legistar.com/View.ashx?GUID=3CF40CBF-6D84-404B-AC8F-E19C01161F5C&ID=15331701&M=F), PDF page 33.

## Source coverage

The initial manifest covers 3 of 539 jurisdictions. The public HCD APR package does not expose a Table B dataset. HCD’s blank workbook contains historical lookup data, but it is not a complete set of submitted current-year reports. It has missing current-cycle matches, duplicate keys with differing values, and projection rows without cycle identifiers. Its footer warns that historical Table F/F2 credits are excluded, yet some corrected historical entries already include them. Adding those credits again would double count. Treating unmatched rows as zero or combining those seeds with A2 would not establish complete submitted Table B totals.

The source collection therefore remains an input requirement, not something the importer silently estimates. [HCD's APR instructions](https://www.hcd.ca.gov/sites/default/files/docs/planning-and-community/housing-element-annual-progress-report-instructions.pdf), page 20, explain the inclusion of the projection period in Table B.

## Publication work still required

The pace comparison needs separately managed APR reporting-cutoff and planning-period dates. They must not be inferred from a refresh timestamp or from unrelated tracker date fields. Preserve the existing public field identities and status labels when changing their formulas, use `>=` for the pace threshold, and make missing inputs unavailable.

Config version 2 extends the guarded Airtable publisher to APR dates alongside the existing permit bands. Existing legal-policy dates must not change as a side effect. This correction does not depend on replacing the permit source with Table B. A future change to RHNA credited totals still requires a reconciled source ledger and its own complete comparison.
