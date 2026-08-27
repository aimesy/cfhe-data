from __future__ import annotations

import argparse
import json

from cfhe_data import cli
from cfhe_data.cli import build_parser


def test_refresh_requires_explicit_cutoff_year() -> None:
    parser = build_parser()
    args = parser.parse_args(["refresh", "--cutoff-year", "2025"])

    assert args.command == "refresh"
    assert args.cutoff_year == 2025


def test_no_publish_command_exists() -> None:
    parser = build_parser()
    choices = parser._subparsers._group_actions[0].choices

    assert "webflow-plan" in choices
    assert "airtable-plan" in choices
    assert "webflow-apply" not in choices
    assert "airtable-apply" not in choices
    assert "publish" not in choices


def test_airtable_apply_reports_api_client_result_fields(
    tmp_path, monkeypatch, capsys
) -> None:
    totals_path = tmp_path / "totals.json"
    config_path = tmp_path / "config.json"
    cycle_policy_path = tmp_path / "cycle-policy.json"
    totals_path.write_text("{}", encoding="utf-8")
    config_path.write_text("{}", encoding="utf-8")
    cycle_policy_path.write_text("{}", encoding="utf-8")

    class StubClient:
        def update_records(self, updates, *, verify_schema):
            assert updates == []
            assert verify_schema is False
            return {
                "updated_record_count": 2,
                "already_current_record_count": 3,
                "verified_record_count": 5,
                "batch_count": 1,
            }

    snapshots = iter(
        [
            ({"stage": "initial"}, None),
            ({"stage": "current"}, StubClient()),
            ({"stage": "final"}, None),
        ]
    )
    plans = iter(
        [
            {"apply_eligible": True, "change_count": 5},
            {"apply_eligible": True, "change_count": 0},
        ]
    )
    monkeypatch.setenv("AIRTABLE_TOKEN", "test-token")
    monkeypatch.setattr(
        cli, "fetch_airtable_snapshot", lambda **kwargs: next(snapshots)
    )
    monkeypatch.setattr(cli, "build_airtable_sync_plan", lambda **kwargs: next(plans))
    monkeypatch.setattr(cli, "verify_airtable_sync_plan", lambda *args: None)
    monkeypatch.setattr(cli, "plan_record_updates", lambda plan: [])
    monkeypatch.setattr(
        cli,
        "summarize_airtable_sync_plan",
        lambda plan: {"change_count": plan["change_count"]},
    )
    args = argparse.Namespace(
        totals=totals_path,
        config=config_path,
        cycle_policy=cycle_policy_path,
        git_sha="a" * 40,
        plan_output=None,
        apply=True,
    )

    assert cli._airtable_sync(args) == 0
    assert json.loads(capsys.readouterr().out) == {
        "mode": "applied",
        "change_count": 0,
        "updated_count": 2,
        "already_current_count": 3,
        "verified_count": 5,
        "batch_count": 1,
    }
