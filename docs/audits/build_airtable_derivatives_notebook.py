"""Build and execute the Airtable derivatives audit notebook.

visibility: non-public:private
classification: archive-internal
"""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path

import nbformat


def build_notebook(repo_root: Path) -> Path:
    """Create an executed notebook from the latest aggregate audit result."""

    output_path = repo_root / "docs" / "audits" / "airtable_derivatives_audit.ipynb"
    result_path = repo_root / "data" / "airtable" / "derivatives_audit_results.json"
    audit_snapshot = json.loads(result_path.read_text(encoding="utf-8"))
    summary_snapshot = audit_snapshot["summary"]
    failure_count = summary_snapshot["check_counts"].get("fail", 0)
    warning_count = summary_snapshot["check_counts"].get("warn", 0)
    case_repeat_events = summary_snapshot.get(
        "event_unique_case_normalized_duplicate_token_event_count", 0
    )
    if failure_count:
        tldr = (
            f"The full read-only audit found {failure_count} failing checks and "
            f"{warning_count} warnings. The tables below identify the affected chains."
        )
    else:
        tldr = (
            "The repaired Current, housing-element, Builder’s Remedy, Rent, RHNA "
            "Prediction, Reports, County, Census, and Event-email derivative chains "
            "reconcile independently. "
            f"The only classified source-normalization issue is {case_repeat_events} "
            "Events that retain case-only email repeats; exact sets and exact-token "
            "uniqueness still pass."
        )
    cells = [
        nbformat.v4.new_markdown_cell(
            "# Airtable Passthrough and Derivative Audit\n\n"
            "Read-only audit of the `cfhe-data` repository and the live CFHE Airtable base."
        ),
        nbformat.v4.new_markdown_cell("## tl;dr\n\n" + tldr),
        nbformat.v4.new_markdown_cell(
            "## Context & Methods\n\n"
            "The audit reads Airtable schema, records, interfaces, forms, views, and automation "
            "configuration without calling mutation endpoints. It independently recomputes "
            "links, counts, rollups, Current-gated housing element passthroughs, zero-safe "
            "formulas, Census keys, exact Event email sets and duplicate tokens, nine County "
            "interface pages, repository derivatives, and the production sync dry run. The notebook "
            "loads the aggregate result emitted by `airtable_derivatives_audit.py`; set "
            "`REFRESH_LIVE` below to rerun that collection step."
        ),
        nbformat.v4.new_code_cell(
            "import json\n"
            "import subprocess\n"
            "from pathlib import Path\n\n"
            "cursor = Path.cwd().resolve()\n"
            "for candidate in (cursor, *cursor.parents):\n"
            "    if (candidate / 'pyproject.toml').exists() and (candidate / 'src' / 'cfhe_data').exists():\n"
            "        REPO_ROOT = candidate\n"
            "        break\n"
            "else:\n"
            "    raise RuntimeError('Could not locate the cfhe-data repository root')\n\n"
            "REFRESH_LIVE = False\n"
            "if REFRESH_LIVE:\n"
            "    subprocess.run([\n"
            "        'uv', 'run', '--with', 'requests', '--with-editable', '.', '--extra', 'dev',\n"
            "        'python', 'docs/audits/airtable_derivatives_audit.py'\n"
            "    ], cwd=REPO_ROOT, check=True)\n\n"
            "result_path = REPO_ROOT / 'data' / 'airtable' / 'derivatives_audit_results.json'\n"
            "audit = json.loads(result_path.read_text(encoding='utf-8'))\n"
            "print(f\"Loaded read-only audit executed at {audit['executed_at']}\")"
        ),
        nbformat.v4.new_markdown_cell("## Data"),
        nbformat.v4.new_code_cell(
            "summary = audit['summary']\n"
            "record_total = sum(summary['table_record_counts'].values())\n"
            "coverage = {\n"
            "    'Airtable tables': summary['table_count'],\n"
            "    'Airtable records': record_total,\n"
            "    'Fields': summary['field_count'],\n"
            "    'Computed fields': summary['computed_field_count'],\n"
            "    'Linked-record fields': summary['link_field_count'],\n"
            "    'Views': summary['view_count'],\n"
            "    'Interface pages': summary['interface_page_count'],\n"
            "    'Automations': summary['automation_count'],\n"
            "}\n"
            "for label, value in coverage.items():\n"
            "    print(f'{label:24} {value:,}')\n"
            "print(f\"Managed permit total      {summary['live_permit_total']:,}\")\n"
            "print(f\"Reviewed source total     {summary['desired_permit_total']:,}\")"
        ),
        nbformat.v4.new_markdown_cell("## Results"),
        nbformat.v4.new_code_cell(
            "print('Overall status:', summary['overall_status'].upper())\n"
            "print('Checks by status:', summary['check_counts'])\n"
            "print('Issues by severity:', summary['issue_severity_counts'])\n\n"
            "rank = {'high': 0, 'medium': 1, 'low': 2, 'info': 3}\n"
            "nonpass = sorted(\n"
            "    (check for check in audit['checks'] if check['status'] != 'pass'),\n"
            "    key=lambda check: (rank[check['severity']], check['domain'], check['check']),\n"
            ")\n"
            "print('\\n| Severity | Domain | Check | Instances | Evidence |')\n"
            "print('|---|---|---|---:|---|')\n"
            "for check in nonpass:\n"
            "    evidence = check['evidence'].replace('|', '/')\n"
            "    print(f\"| {check['severity']} | {check['domain']} | {check['check']} | {check['failures']:,} | {evidence} |\")\n"
        ),
        nbformat.v4.new_code_cell(
            "checks_by_name = {check['check']: check for check in audit['checks']}\n"
            "core_assertions = [\n"
            "    ('Live permit total equals reviewed source', summary['live_permit_total'] == summary['desired_permit_total']),\n"
            "    ('Production dry run reports zero changes', summary['sync_summary'].get('change_count') == 0),\n"
            "    ('All intended jurisdictions matched', summary['sync_summary'].get('matched_count') == 539),\n"
            "    ('Current cycle split is 522 sixth and 17 seventh', summary['current_cycle_counts'] == {'6th': 522, '7th': 17}),\n"
            "    ('Current helpers and HE passthroughs have zero mismatches', summary['current_helper_mismatch_count'] == 0 and summary['he_passthrough_mismatch_count'] == 0),\n"
            "    ('Both Builder outputs have zero mismatches', summary['builder_text_mismatch_count'] == 0 and summary['builder_flag_mismatch_count'] == 0),\n"
            "    ('Rent outputs have zero mismatches', summary['rent_overview_mismatch_count'] == 0 and summary['rent_text_mismatch_count'] == 0),\n"
            "    ('Prediction, lookup, and color have zero mismatches', summary['rhna_prediction_mismatch_count'] == 0 and summary['prediction_lookup_mismatch_count'] == 0 and summary['prediction_color_mismatch_count'] == 0),\n"
            "    ('Reports ratios have zero mismatches and errors', summary['pro_housing_mismatch_count'] == 0 and summary['pro_housing_error_count'] == 0),\n"
            "    ('Census keys are unique', summary['census_link_violation_count'] == 0 and summary['census_duplicate_group_count'] == 0),\n"
            "    ('Event exact sets and exact-token uniqueness pass', summary['event_lookup_set_mismatch_count'] == 0 and summary['event_unique_formula_set_mismatch_count'] == 0 and summary['event_unique_exact_duplicate_token_event_count'] == 0),\n"
            "    ('Counties interface has nine successful page probes', summary['county_interface_page_count'] == 9 and summary['county_interface_probe_success_count'] == 9),\n"
            "]\n"
            "for label, passed in core_assertions:\n"
            "    print(('PASS' if passed else 'FAIL').ljust(6), label)"
        ),
        nbformat.v4.new_code_cell(
            "repaired_evidence = {\n"
            "    'Current RHNA rows': summary['current_rhna_count'],\n"
            "    'Current helper mismatches': summary['current_helper_mismatch_count'],\n"
            "    'HE passthrough mismatches': summary['he_passthrough_mismatch_count'],\n"
            "    'Builder text + flag mismatches': summary['builder_text_mismatch_count'] + summary['builder_flag_mismatch_count'],\n"
            "    'Rent mismatches': summary['rent_overview_mismatch_count'] + summary['rent_text_mismatch_count'],\n"
            "    'Prediction-chain mismatches': summary['rhna_prediction_mismatch_count'] + summary['prediction_lookup_mismatch_count'] + summary['prediction_color_mismatch_count'],\n"
            "    'Reports mismatches + errors': summary['pro_housing_mismatch_count'] + summary['pro_housing_error_count'],\n"
            "    'Census duplicate groups': summary['census_duplicate_group_count'],\n"
            "    'Event exact-set mismatches': summary['event_lookup_set_mismatch_count'] + summary['event_unique_formula_set_mismatch_count'],\n"
            "    'Event exact duplicate-token records': summary['event_unique_exact_duplicate_token_event_count'],\n"
            "    'Event case-only source repeats': summary['event_unique_case_normalized_duplicate_token_event_count'],\n"
            "    'Successful County interface probes': summary['county_interface_probe_success_count'],\n"
            "}\n"
            "for label, value in repaired_evidence.items():\n"
            "    print(f'{label:42} {value:,}')"
        ),
        nbformat.v4.new_markdown_cell(
            "## Takeaways\n\n"
            "1. The Current flag is cycle agnostic and resolves 522 sixth-cycle and 17 "
            "seventh-cycle jurisdictions through the same helper and rollup chain.\n"
            "2. Housing element, Builder’s Remedy, Rent, Prediction, Reports, County, Census, "
            "and Event derivatives are now checked by independent value recomputation, not only "
            "by Airtable schema validity.\n"
            "3. Event email validation uses exact source sets and exact duplicate-token counts, "
            "so it detects truncation without reinstating an arbitrary threshold.\n"
            "4. Case-only email repeats remain classified as source normalization because the "
            "derivative chain preserves the exact source spellings.\n"
            "5. Interface layout and automation configuration are inspectable, but automation "
            "run history and connected-account health remain outside this audit identity’s access."
        ),
    ]

    notebook = nbformat.v4.new_notebook(
        cells=cells,
        metadata={
            "classification": "archive-internal",
            "visibility": "non-public:private",
            "kernelspec": {
                "display_name": "Python 3",
                "language": "python",
                "name": "python3",
            },
            "language_info": {"name": "python", "version": "3"},
        },
    )
    namespace: dict[str, object] = {}
    execution_count = 0
    original_cwd = Path.cwd()
    try:
        os.chdir(repo_root)
        for cell in notebook.cells:
            if cell.cell_type != "code":
                continue
            execution_count += 1
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                exec(  # noqa: S102
                    compile(
                        cell.source,
                        f"{output_path.name}:cell-{execution_count}",
                        "exec",
                    ),
                    namespace,
                )
            cell.execution_count = execution_count
            cell.outputs = [
                nbformat.v4.new_output(
                    output_type="stream", name="stdout", text=output.getvalue()
                )
            ]
    finally:
        os.chdir(original_cwd)
    nbformat.validate(notebook)
    nbformat.write(notebook, output_path)
    return output_path


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[2]
    print(build_notebook(root))
