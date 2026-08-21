from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def test_refresh_workflow_does_not_publish_artifacts() -> None:
    workflow = (REPOSITORY_ROOT / ".github/workflows/refresh-hcd.yml").read_text(
        encoding="utf-8"
    )

    assert "actions/upload-artifact" not in workflow
    assert "data/raw/*.csv" not in workflow
    assert "data/run/**" not in workflow
