# Provenance

## Official resources

- HCD APR dashboard and downloads: <https://www.hcd.ca.gov/housing-open-data-tools/apr-dashboard>
- APR Table A2 resource: `fe505d9b-8c36-42ba-ba30-08bc4f34e022`
- Sixth cycle RHNA progress resource: `1e80a9cf-724c-432d-8374-e9708a6a92dc`

The configured download URLs live in `config/sources.toml`. Resource identifiers are the stable anchors. Redirect targets and response metadata are captured anew for every source digest.

## Verified development snapshot

The initial deduplication work was independently checked against the HCD snapshot retrieved on August 20, 2026, whose portal update date was August 14, 2026:

- Table A2 SHA-256: `e55cf368edd234fa1a513a007cb3bf95d8b9fd50e1cff2256fb324c02ed4a685`
- Sixth cycle RHNA SHA-256: `d6dac896a1e7557b541c0875aeb951d78da3ac03476f966de8b00ebffba6fe89`
- selected rows before deduplication: 311,122
- selected permit units before deduplication: 563,376
- rows after the audited rules: 277,716
- units after the audited rules: 501,770
- units retained from rows with no permit date: 640

These figures are a regression record for those exact source digests. They are not production constants and must not be used to force a later source to match.

## First live repository refresh

HCD updated both resources on August 21, 2026. The live source audit found:

- Table A2 uploaded CSV SHA-256: `2ac7a83a0b79feceffabbe024a3a7ea9279eb34bd4e912f631b13a07b15b0c4a`
- Table A2 uploaded CSV MD5: `adc97b4aa31f815cc28e5ee14f99a5b1`
- Table A2: 292,500,176 bytes, 69 columns, and 921,393 logical records
- Sixth cycle RHNA uploaded CSV SHA-256: `196627e705a4d4488eeb5e6b0f886966bc85ecc88d539de77d197dee910e4c1f`
- Sixth cycle RHNA uploaded CSV MD5: `4298fb9961b56c65f48b2fe5e4bee019`
- Sixth cycle RHNA: 47,526 bytes and 539 logical records

The uploaded files and DataStore converged on their ordered fields and logical record counts. The portal’s advertised byte sizes were wrong for both files, so byte size is recorded but is not used as the only integrity gate.

The first live build selected 311,150 rows and 563,435 units. Under deduplication profile v1, it retained 277,784 rows and 501,913 units. Holding the v1 rules constant, the source change from August 20 added 79 units in Los Angeles, 61 in Corcoran, 2 in Placer County, and 1 in Willits. No other jurisdiction total changed.

## Conservative residual rule revision

The release manifest binds the official DataStore dump files for the same August 21 logical snapshot:

- Table A2 dump SHA-256: `8529a407a14a418c95b67d0e319c4834537220fbd29e393c209d9d631c0d680e`, 297,917,433 bytes
- Sixth cycle RHNA dump SHA-256: `36aa66ec9555878ba92071a13ee7416e03274e280b7b8ee3de858153ab40a364`, 49,039 bytes

The DataStore dumps include a synthetic identifier column, so their bytes and digests differ from the uploaded CSV files listed above. The pinned schemas remove only that synthetic column. The remaining ordered fields and logical record counts match the resource metadata.

The release profile is `audited-apr-snapshots-v3`. It does not accept a house number, street, or project name as sufficient residual corroboration by itself. Every earlier row must match a specific retained row using an exact valid APN, exact valid coordinates, or at least two independent signals among substantive project metadata, street, and house number. Conflicting populated unit identifiers veto removal. Component edge ordering is fixed by the immutable source record index, so input order cannot change the result.

Compared with v1 on the same logical snapshot, v3 retained 476 additional rows and 787 additional units. The change restored 68 very low, 72 low, 187 moderate, and 460 above moderate units. Acutely low and extremely low totals did not change.

The release build retained 278,260 of 311,150 selected rows and 502,700 of 563,435 selected units. It removed 32,890 earlier snapshot rows containing 60,735 units. The output contains 539 jurisdictions and 640 units from rows with no permit date.

## Final retained lineage revision

The August 27, 2026 lineage audit found that 808 removal entries named at least one directly matched row that a later rule also removed. Those entries contained 821 references to rows that did not survive the final rule pass. Separately, 23 reference values across 22 removal entries did not agree with the single retained report year recorded on the entry. Every affected chain was acyclic and terminated in a later retained row, so the defect concerned review lineage rather than deduplication counts.

The revised ledger preserves the directly matched source indices and separately resolves the final retained source indices after every rule pass. The standard and mixed-cycle ledgers now contain zero final references to removed rows, zero missing final references, and zero final reference years that disagree with the declared retained report year. Each ledger contains 808 entries marked as transitive lineage. The change did not alter the standard total of 502,700 units or the mixed-cycle Airtable total of 501,601 units.
