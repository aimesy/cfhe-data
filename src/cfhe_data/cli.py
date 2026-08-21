"""Command line interface for CFHE data refreshes and review plans."""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .pipeline import build_artifacts
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
    print(
        json.dumps(
            {
                "selected_rows": artifacts.selected_rows,
                "retained_rows": artifacts.retained_rows,
                "removed_rows": artifacts.removed_rows,
                "total_units": artifacts.total_units,
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
    build.set_defaults(handler=_build)

    plan = subparsers.add_parser(
        "webflow-plan", help="Create a read only plan from a supplied CMS snapshot"
    )
    plan.add_argument("--totals", type=Path, required=True)
    plan.add_argument("--cms-snapshot", type=Path, required=True)
    plan.add_argument("--mapping", type=Path, required=True)
    plan.add_argument("--output", type=Path, required=True)
    plan.set_defaults(handler=_webflow_plan)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
