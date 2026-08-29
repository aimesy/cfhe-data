"""Command line interface for CFHE data refreshes and review plans."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .airtable import build_airtable_change_plan, verify_airtable_change_plan
from .airtable_sync import (
    AirtableSyncBlockedError,
    build_airtable_sync_plan,
    fetch_airtable_snapshot,
    plan_record_updates,
    plan_verification_updates,
    summarize_airtable_sync_plan,
    verify_airtable_sync_plan,
)
from .pipeline import build_airtable_artifacts, build_artifacts
from .source import fetch_source, load_specs, stable_manifest
from .webflow import build_change_plan, verify_change_plan


def _read_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_object(path: Path, label: str) -> dict[str, object]:
    value = _read_json(path)
    if not isinstance(value, dict):
        raise TypeError(f"{label} must contain a JSON object: {path}")
    return value


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _project_root(sources_path: Path) -> Path:
    resolved = sources_path.resolve()
    if resolved.parent.name == "config":
        return resolved.parent.parent
    return Path.cwd().resolve()


def _project_path(project_root: Path, path: Path) -> Path:
    return path if path.is_absolute() else project_root / path


def _refresh(args: argparse.Namespace) -> int:
    sources_path = args.sources.resolve()
    project_root = _project_root(sources_path)
    specs = load_specs(sources_path)
    specs_by_name = {spec.name: spec for spec in specs}
    required = {"table_a2", "rhna_progress_6"}
    missing = sorted(required - set(specs_by_name))
    if missing:
        raise ValueError("Source configuration is missing: " + ", ".join(missing))

    args.raw_dir.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=min(4, len(specs))) as pool:
        futures = {
            spec.name: pool.submit(fetch_source, spec, args.raw_dir) for spec in specs
        }
        results = {name: future.result() for name, future in futures.items()}

    manifest_path = args.output_dir / "source_manifest.json"
    previous: Mapping[str, object] | None = None
    if manifest_path.exists():
        previous = _read_object(manifest_path, "Previous source manifest")
    manifest = stable_manifest(list(results.values()), previous=previous)

    table_spec = specs_by_name["table_a2"]
    rhna_spec = specs_by_name["rhna_progress_6"]
    artifacts = build_artifacts(
        table_a2_path=results["table_a2"].path,
        rhna_path=results["rhna_progress_6"].path,
        table_contract_path=project_root / table_spec.schema,
        rhna_contract_path=project_root / rhna_spec.schema,
        output_schema_path=project_root / "schemas/jurisdiction_totals.json",
        source_manifest=manifest,
        cutoff_year=args.cutoff_year,
        output_dir=args.output_dir,
        audit_dir=args.audit_dir,
    )
    airtable_artifacts = build_airtable_artifacts(
        table_a2_path=results["table_a2"].path,
        rhna_path=results["rhna_progress_6"].path,
        table_contract_path=project_root / table_spec.schema,
        rhna_contract_path=project_root / rhna_spec.schema,
        output_schema_path=_project_path(project_root, args.airtable_output_schema),
        cycle_policy_path=_project_path(project_root, args.airtable_cycle_policy),
        source_manifest=manifest,
        cutoff_year=args.cutoff_year,
        output_dir=args.output_dir,
        audit_dir=args.audit_dir,
    )
    print(
        json.dumps(
            {
                "selected_rows": artifacts.selected_rows,
                "retained_rows": artifacts.retained_rows,
                "removed_rows": artifacts.removed_rows,
                "total_units": artifacts.total_units,
                "jurisdiction_json": str(artifacts.jurisdiction_json),
                "audit_summary": str(artifacts.audit_summary),
                "decision_ledger": str(artifacts.decision_ledger),
                "review_ledger": str(artifacts.review_ledger),
                "airtable_totals": str(airtable_artifacts.jurisdiction_json),
                "airtable_audit_summary": str(airtable_artifacts.audit_summary),
                "airtable_review_ledger": str(airtable_artifacts.review_ledger),
            },
            indent=2,
        )
    )
    return 0


def _build(args: argparse.Namespace) -> int:
    manifest = _read_object(args.manifest, "Source manifest")
    artifacts = build_artifacts(
        table_a2_path=args.table_a2,
        rhna_path=args.rhna,
        table_contract_path=args.table_contract,
        rhna_contract_path=args.rhna_contract,
        output_schema_path=args.output_schema,
        source_manifest=manifest,
        cutoff_year=args.cutoff_year,
        output_dir=args.output_dir,
        audit_dir=args.audit_dir,
    )
    airtable_artifacts = build_airtable_artifacts(
        table_a2_path=args.table_a2,
        rhna_path=args.rhna,
        table_contract_path=args.table_contract,
        rhna_contract_path=args.rhna_contract,
        output_schema_path=args.airtable_output_schema,
        cycle_policy_path=args.airtable_cycle_policy,
        source_manifest=manifest,
        cutoff_year=args.cutoff_year,
        output_dir=args.output_dir,
        audit_dir=args.audit_dir,
    )
    print(
        json.dumps(
            {
                "selected_rows": artifacts.selected_rows,
                "retained_rows": artifacts.retained_rows,
                "removed_rows": artifacts.removed_rows,
                "total_units": artifacts.total_units,
                "airtable_total_units": airtable_artifacts.total_units,
                "airtable_totals": str(airtable_artifacts.jurisdiction_json),
                "review_ledger": str(artifacts.review_ledger),
                "airtable_review_ledger": str(airtable_artifacts.review_ledger),
            },
            indent=2,
        )
    )
    return 0


def _webflow_plan(args: argparse.Namespace) -> int:
    totals = _read_object(args.totals, "Jurisdiction totals")
    snapshot = _read_json(args.cms_snapshot)
    config = _read_object(args.mapping, "Webflow mapping")
    plan = build_change_plan(totals, snapshot, config)
    verify_change_plan(plan, snapshot)
    _write_json(args.output, plan)
    print(
        json.dumps(
            {
                "plan": str(args.output),
                "plan_sha256": plan["plan_sha256"],
                "change_count": plan["change_count"],
            },
            indent=2,
        )
    )
    return 0


def _airtable_plan(args: argparse.Namespace) -> int:
    totals = _read_object(args.totals, "Jurisdiction totals")
    snapshot = _read_object(args.airtable_snapshot, "Airtable snapshot")
    config = _read_object(args.mapping, "Airtable mapping")
    plan = build_airtable_change_plan(totals, snapshot, config)
    verify_airtable_change_plan(plan, snapshot)
    _write_json(args.output, plan)
    print(
        json.dumps(
            {
                "plan": str(args.output),
                "plan_sha256": plan["plan_sha256"],
                "apply_eligible": plan["apply_eligible"],
                "change_count": plan["change_count"],
                "blocker_count": len(plan["blockers"]),
            },
            indent=2,
        )
    )
    return 0


def _airtable_sync(args: argparse.Namespace) -> int:
    token = os.environ.get("AIRTABLE_TOKEN", "")
    if not token:
        raise AirtableSyncBlockedError(
            "AIRTABLE_TOKEN is required and must be supplied through the environment"
        )
    totals = _read_object(args.totals, "Airtable totals")
    config = _read_object(args.config, "Airtable sync config")
    cycle_policy = _read_object(args.cycle_policy, "Airtable cycle policy")
    snapshot, _client = fetch_airtable_snapshot(token=token, config=config)
    plan = build_airtable_sync_plan(
        totals=totals,
        snapshot=snapshot,
        config=config,
        cycle_policy=cycle_policy,
        git_sha=args.git_sha,
    )
    if args.plan_output is not None:
        _write_json(args.plan_output, plan)
    summary = summarize_airtable_sync_plan(plan)
    if not args.apply:
        print(json.dumps({"mode": "dry_run", **summary}, indent=2))
        return 0
    if plan["apply_eligible"] is not True:
        print(json.dumps({"mode": "blocked", **summary}, indent=2))
        raise AirtableSyncBlockedError("Airtable publication is blocked")

    current_snapshot, current_client = fetch_airtable_snapshot(
        token=token, config=config
    )
    verify_airtable_sync_plan(plan, current_snapshot)
    result = current_client.update_records(
        plan_record_updates(plan), verify_schema=False
    )
    final_snapshot, final_client = fetch_airtable_snapshot(token=token, config=config)
    final_plan = build_airtable_sync_plan(
        totals=totals,
        snapshot=final_snapshot,
        config=config,
        cycle_policy=cycle_policy,
        git_sha=args.git_sha,
    )
    if final_plan["apply_eligible"] is not True or final_plan["change_count"] != 0:
        raise AirtableSyncBlockedError(
            "Airtable readback does not fully match the reviewed source"
        )
    verified_at = (
        dt.datetime.now(dt.UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    verification_result = final_client.update_records(
        plan_verification_updates(final_plan, verified_at),
        verify_schema=False,
        patch_response_is_final=True,
    )
    print(
        json.dumps(
            {
                "mode": "applied",
                **summarize_airtable_sync_plan(final_plan),
                "updated_count": result["updated_record_count"],
                "already_current_count": result["already_current_record_count"],
                "verified_count": result["verified_record_count"],
                "batch_count": result["batch_count"],
                "apr_verified_at": verified_at,
                "verification_updated_count": verification_result[
                    "updated_record_count"
                ],
                "verification_verified_count": verification_result[
                    "verified_record_count"
                ],
                "verification_batch_count": verification_result["batch_count"],
            },
            indent=2,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cfhe-data",
        description="Build reviewed CFHE jurisdiction permit totals from HCD APR data.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    refresh = subparsers.add_parser(
        "refresh", help="Fetch official sources and rebuild outputs"
    )
    refresh.add_argument("--sources", type=Path, default=Path("config/sources.toml"))
    refresh.add_argument("--raw-dir", type=Path, default=Path("data/raw"))
    refresh.add_argument("--output-dir", type=Path, default=Path("data/processed"))
    refresh.add_argument("--audit-dir", type=Path, default=Path("data/run"))
    refresh.add_argument("--cutoff-year", type=int, required=True)
    refresh.add_argument(
        "--airtable-cycle-policy",
        type=Path,
        default=Path("config/airtable_cycles.json"),
    )
    refresh.add_argument(
        "--airtable-output-schema",
        type=Path,
        default=Path("schemas/airtable_totals.json"),
    )
    refresh.set_defaults(handler=_refresh)

    build = subparsers.add_parser(
        "build", help="Build from existing local source files"
    )
    build.add_argument("--table-a2", type=Path, required=True)
    build.add_argument("--rhna", type=Path, required=True)
    build.add_argument("--manifest", type=Path, required=True)
    build.add_argument(
        "--table-contract", type=Path, default=Path("schemas/table_a2.json")
    )
    build.add_argument(
        "--rhna-contract", type=Path, default=Path("schemas/rhna_progress_6.json")
    )
    build.add_argument(
        "--output-schema",
        type=Path,
        default=Path("schemas/jurisdiction_totals.json"),
    )
    build.add_argument("--output-dir", type=Path, default=Path("data/processed"))
    build.add_argument("--audit-dir", type=Path, default=Path("data/run"))
    build.add_argument("--cutoff-year", type=int, required=True)
    build.add_argument(
        "--airtable-cycle-policy",
        type=Path,
        default=Path("config/airtable_cycles.json"),
    )
    build.add_argument(
        "--airtable-output-schema",
        type=Path,
        default=Path("schemas/airtable_totals.json"),
    )
    build.set_defaults(handler=_build)

    plan = subparsers.add_parser(
        "webflow-plan", help="Create a read only plan from a supplied CMS snapshot"
    )
    plan.add_argument("--totals", type=Path, required=True)
    plan.add_argument("--cms-snapshot", type=Path, required=True)
    plan.add_argument("--mapping", type=Path, required=True)
    plan.add_argument("--output", type=Path, required=True)
    plan.set_defaults(handler=_webflow_plan)

    airtable_plan = subparsers.add_parser(
        "airtable-plan",
        help="Create a read only comparison from a supplied Airtable snapshot",
    )
    airtable_plan.add_argument("--totals", type=Path, required=True)
    airtable_plan.add_argument("--airtable-snapshot", type=Path, required=True)
    airtable_plan.add_argument("--mapping", type=Path, required=True)
    airtable_plan.add_argument("--output", type=Path, required=True)
    airtable_plan.set_defaults(handler=_airtable_plan)

    airtable_sync = subparsers.add_parser(
        "airtable-sync",
        help="Fetch Airtable, build a guarded mixed-cycle plan, and optionally apply it",
    )
    airtable_sync.add_argument(
        "--totals", type=Path, default=Path("data/processed/airtable_totals.json")
    )
    airtable_sync.add_argument(
        "--config", type=Path, default=Path("config/airtable_sync.json")
    )
    airtable_sync.add_argument(
        "--cycle-policy", type=Path, default=Path("config/airtable_cycles.json")
    )
    airtable_sync.add_argument("--git-sha", required=True)
    airtable_sync.add_argument("--plan-output", type=Path)
    airtable_sync.add_argument(
        "--apply",
        action="store_true",
        help="Apply an eligible plan; without this flag the command is read only",
    )
    airtable_sync.set_defaults(handler=_airtable_sync)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
