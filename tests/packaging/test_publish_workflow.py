"""Static contracts for the release publishing workflow."""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).parents[2]


def _publish_workflow() -> dict[str, object]:
    return yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "publish.yml").read_text())


def _workflow_step_script(step_name: str) -> str:
    workflow = _publish_workflow()
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    matching_steps = [
        step
        for job in jobs.values()
        if isinstance(job, dict)
        for step in job.get("steps", [])
        if step.get("name") == step_name
    ]
    assert len(matching_steps) == 1
    script = matching_steps[0].get("run")
    assert isinstance(script, str)
    return script


def test_publish_workflow_pins_every_setup_uv_binary_version() -> None:
    workflow = _publish_workflow()
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    setup_uv_steps = [
        step
        for job in jobs.values()
        if isinstance(job, dict)
        for step in job.get("steps", [])
        if step.get("uses", "").startswith("astral-sh/setup-uv@")
    ]

    assert setup_uv_steps
    assert {step.get("with", {}).get("version") for step in setup_uv_steps} == {"0.9.24"}


def test_publish_workflow_builds_one_universal_wheel() -> None:
    workflow_text = (REPO_ROOT / ".github" / "workflows" / "publish.yml").read_text()

    assert "uv build --wheel --out-dir dist/release" in workflow_text
    assert "packaging/check_release_version.py --wheel-dir dist/release" in workflow_text
    assert "Require exactly one wheel and no sdist" in workflow_text


def test_installed_wheel_check_uses_distinct_clean_install_and_runtime_homes() -> None:
    script = _workflow_step_script("Verify wheel in a clean installation")

    assert 'install_home="$runtime_root/install-home"' in script
    assert 'verify_home="$runtime_root/verify-home"' in script
    assert 'test -z "$(find "$install_home" -mindepth 1 -print -quit)"' in script
    assert 'test -z "$(find "$verify_home" -mindepth 1 -print -quit)"' in script
    assert script.count('HOME="$install_home"') == 1
    assert script.count('HOME="$verify_home"') == 1
    assert script.index('find "$install_home"') < script.index('HOME="$install_home"')
    assert script.index('find "$verify_home"') < script.index('HOME="$verify_home"')
    assert '--constraints "$RUNNER_TEMP/release-verify-constraints.txt"' in script


def test_publish_workflow_ignores_vibesys_cli_releases() -> None:
    # `publish` needs `build`, so skipping `build` skips the PyPI upload too.
    workflow = _publish_workflow()
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    assert jobs["publish"]["needs"] == "build"
    condition = jobs["build"]["if"]
    assert "startsWith(github.event.release.tag_name, 'cli-')" in condition
    assert "github.event_name != 'release'" in condition
