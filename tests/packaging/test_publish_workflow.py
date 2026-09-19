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


def test_publish_workflow_runs_pinned_auditwheel_on_linux_release_wheels() -> None:
    workflow_text = (REPO_ROOT / ".github" / "workflows" / "publish.yml").read_text()

    assert "uv run auditwheel show" in workflow_text
    assert "uv run --with auditwheel" not in workflow_text


def test_installed_wheel_check_uses_distinct_clean_install_and_runtime_homes() -> None:
    script = _workflow_step_script("Verify the installed wheel with a sanitized PATH")

    assert 'install_home="$runtime_root/install-home"' in script
    assert 'verify_home="$runtime_root/verify-home"' in script
    assert 'test -z "$(find "$install_home" -mindepth 1 -print -quit)"' in script
    assert 'test -z "$(find "$verify_home" -mindepth 1 -print -quit)"' in script
    assert script.count('HOME="$install_home"') == 1
    assert script.count('HOME="$verify_home"') == 1
    assert script.index('find "$install_home"') < script.index('HOME="$install_home"')
    assert script.index('find "$verify_home"') < script.index('HOME="$verify_home"')


def test_docker_check_keeps_runtime_home_clean_during_installation() -> None:
    dockerfile = (REPO_ROOT / "packaging" / "release-wheel.Dockerfile").read_text()

    assert "HOME=/tmp/verify-home" in dockerfile
    assert "INSTALL_HOME=/tmp/install-home" in dockerfile
    assert 'test -z "$(find "$INSTALL_HOME" -mindepth 1 -print -quit)"' in dockerfile
    assert 'HOME="$INSTALL_HOME" uv tool install' in dockerfile
    assert 'test -z "$(find "$HOME" -mindepth 1 -print -quit)"' in dockerfile
    assert dockerfile.index('find "$INSTALL_HOME"') < dockerfile.index(
        'HOME="$INSTALL_HOME" uv tool install'
    )


def test_docker_check_provides_the_git_runtime_prerequisite() -> None:
    dockerfile = (REPO_ROOT / "packaging" / "release-wheel.Dockerfile").read_text()

    assert "apt-get install --yes --no-install-recommends git" in dockerfile
    assert "rm -rf /var/lib/apt/lists/*" in dockerfile
