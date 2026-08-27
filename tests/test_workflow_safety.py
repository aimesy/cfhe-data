from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def test_refresh_workflow_uploads_only_compact_review_evidence() -> None:
    workflow = (REPOSITORY_ROOT / ".github/workflows/refresh-hcd.yml").read_text(
        encoding="utf-8"
    )

    assert "actions/upload-artifact@v4" in workflow
    assert "data/run/dedupe_review.jsonl" in workflow
    assert "data/run/airtable_dedupe_review.jsonl" in workflow
    assert "data/processed/audit_summary.json" in workflow
    assert "data/processed/airtable_audit_summary.json" in workflow
    assert "data/raw/*.csv" not in workflow
    assert "data/run/**" not in workflow
    assert "data/run/dedupe_decisions.jsonl" not in workflow
    assert "data/run/airtable_dedupe_decisions.jsonl" not in workflow
    assert "Compact decision ledgers" in workflow
    assert "GITHUB_SERVER_URL" in workflow
    assert "AIRTABLE_TOKEN" not in workflow
    assert "airtable-apply" not in workflow


def test_airtable_publication_runs_only_after_ci_on_master() -> None:
    workflow = (REPOSITORY_ROOT / ".github/workflows/ci.yml").read_text(
        encoding="utf-8"
    )

    assert "publish-airtable:" in workflow
    assert "needs: test" in workflow
    assert "github.event_name == 'push'" in workflow
    assert "github.ref == 'refs/heads/master'" in workflow
    assert "name: airtable-production" in workflow
    assert "cancel-in-progress: false" in workflow
    assert "contents: read" in workflow
    assert "AIRTABLE_TOKEN: ${{ secrets.AIRTABLE_TOKEN }}" in workflow
    assert '--git-sha "$GITHUB_SHA"' in workflow
    assert "--cycle-policy config/airtable_cycles.json" in workflow
    assert "--apply" in workflow
    assert workflow.count('test "$(git rev-parse origin/master)" = "$GITHUB_SHA"') == 2
    assert "pull_request_target" not in workflow
    assert "actions/upload-artifact" not in workflow
