from __future__ import annotations

import argparse
import json

import pytest

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
        def update_records(
            self,
            updates,
            *,
            verify_schema,
            deferred_readback_seconds=0,
            patch_response_is_final=False,
        ):
            assert updates == []
            assert verify_schema is False
            assert deferred_readback_seconds == 0
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
            ({"stage": "final"}, StubClient()),
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
    monkeypatch.setattr(cli, "plan_verification_updates", lambda plan, when: [])
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
    output = json.loads(capsys.readouterr().out)
    verified_at = output.pop("apr_verified_at")
    assert verified_at.endswith(".000Z")
    assert output == {
        "mode": "applied",
        "change_count": 0,
        "updated_count": 2,
        "already_current_count": 3,
        "verified_count": 5,
        "batch_count": 1,
        "verification_updated_count": 2,
        "verification_verified_count": 5,
        "verification_batch_count": 1,
    }


def _series_args(tmp_path, *, apply: bool) -> argparse.Namespace:
    config_path = tmp_path / "series-config.json"
    series_path = tmp_path / "series.json"
    config_path.write_text("{}", encoding="utf-8")
    series_path.write_text("{}", encoding="utf-8")
    return argparse.Namespace(
        config=config_path, series=series_path, plan_output=None, apply=apply
    )


def test_series_sync_requires_a_token(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("AIRTABLE_TOKEN", raising=False)
    with pytest.raises(cli.AirtableSyncBlockedError, match="AIRTABLE_TOKEN"):
        cli._series_sync(_series_args(tmp_path, apply=False))


def test_series_sync_dry_run_never_writes(tmp_path, monkeypatch, capsys) -> None:
    class NoWrites:
        def __getattr__(self, name):
            raise AssertionError(f"dry run called {name}")

    monkeypatch.setenv("AIRTABLE_TOKEN", "test-token")
    monkeypatch.setattr(cli, "fetch_series_snapshot", lambda **kwargs: ({}, NoWrites()))
    monkeypatch.setattr(cli, "build_series_plan", lambda **kwargs: {"change_count": 3})
    monkeypatch.setattr(cli, "summarize_series_plan", lambda plan: dict(plan))

    assert cli._series_sync(_series_args(tmp_path, apply=False)) == 0
    assert json.loads(capsys.readouterr().out) == {
        "mode": "dry_run",
        "change_count": 3,
    }


def test_series_sync_apply_rechecks_then_updates_creates_and_reads_back(
    tmp_path, monkeypatch, capsys
) -> None:
    calls: list[str] = []

    class StubClient:
        def update_records(self, updates, *, verify_schema, patch_response_is_final):
            calls.append("update")
            assert updates == ["update"]
            assert verify_schema is False
            assert patch_response_is_final is True
            return {"updated_record_count": 1, "already_current_record_count": 0}

        def create_records(self, records, *, verify_schema):
            calls.append("create")
            assert records == ["create"]
            assert verify_schema is False
            return {"created_record_count": 1}

    plans = iter(
        [
            {"stage": "initial", "apply_eligible": True, "change_count": 2},
            {"stage": "current", "apply_eligible": True, "change_count": 2},
            {
                "stage": "final",
                "apply_eligible": True,
                "change_count": 0,
                "orphan_count": 1,
            },
        ]
    )

    def plan(**kwargs):
        calls.append("plan")
        return next(plans)

    monkeypatch.setenv("AIRTABLE_TOKEN", "test-token")
    monkeypatch.setattr(
        cli, "fetch_series_snapshot", lambda **kwargs: ({}, StubClient())
    )
    monkeypatch.setattr(cli, "build_series_plan", plan)
    monkeypatch.setattr(
        cli,
        "verify_series_plan_unchanged",
        lambda old, new: calls.append(f"verify {old['stage']} {new['stage']}"),
    )
    monkeypatch.setattr(cli, "series_record_updates", lambda plan, config: ["update"])
    monkeypatch.setattr(cli, "series_record_creates", lambda plan, config: ["create"])
    monkeypatch.setattr(
        cli,
        "summarize_series_plan",
        lambda plan: {"change_count": plan["change_count"]},
    )

    assert cli._series_sync(_series_args(tmp_path, apply=True)) == 0
    assert calls == [
        "plan",
        "plan",
        "verify initial current",
        "update",
        "create",
        "plan",
    ]
    assert json.loads(capsys.readouterr().out) == {
        "mode": "applied",
        "change_count": 2,
        "updated_count": 1,
        "already_current_count": 0,
        "created_count": 1,
        "readback_change_count": 0,
        "readback_orphan_count": 1,
    }


def test_series_sync_apply_stops_before_writing_when_blocked(
    tmp_path, monkeypatch
) -> None:
    class NoWrites:
        def __getattr__(self, name):
            raise AssertionError(f"blocked apply called {name}")

    monkeypatch.setenv("AIRTABLE_TOKEN", "test-token")
    monkeypatch.setattr(cli, "fetch_series_snapshot", lambda **kwargs: ({}, NoWrites()))
    monkeypatch.setattr(
        cli,
        "build_series_plan",
        lambda **kwargs: {"apply_eligible": False, "blockers": ["stray record"]},
    )

    with pytest.raises(cli.AirtableSyncBlockedError, match="stray record"):
        cli._series_sync(_series_args(tmp_path, apply=True))


def test_series_sync_parser_defaults_to_a_dry_run() -> None:
    args = build_parser().parse_args(["series-sync"])
    assert args.handler is cli._series_sync
    assert args.apply is False
    assert args.config.as_posix() == "config/airtable_series_sync.json"
    assert args.series.as_posix() == "data/processed/airtable_series.json"
