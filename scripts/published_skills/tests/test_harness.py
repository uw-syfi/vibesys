from pathlib import Path

from scripts.published_skills.skills import check_identity_assets


def test_identity_assets_reject_only_retired_names(tmp_path: Path) -> None:
    for folder in (
        "skills/search/optimize",
        "orchestrator/sidecar/src/vs_sidecar/prompts",
        "orchestrator/sidecar/tests/goldens",
    ):
        asset = tmp_path / folder / "example.md"
        asset.parent.mkdir(parents=True)
        asset.write_text("VIBESYS_RUN VIBESYS_AGENT VIBESYS_EXEC_ID")
    assert check_identity_assets(tmp_path) == []
    asset.write_text("VIBESYS_RUN_ID\nVIBESYS_AGENT_ID")
    findings = check_identity_assets(tmp_path)
    assert len(findings) == 2
    assert all("example.md:" in str(finding) for finding in findings)
