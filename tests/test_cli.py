from __future__ import annotations

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
    assert "webflow-apply" not in choices
    assert "publish" not in choices
