"""CI package installs fail promptly even when a mirror keeps transferring bytes."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from hypothesis import example, given
from hypothesis import strategies as st

REPO = Path(__file__).resolve().parents[2]
_SHARED_ACTION = ".github/actions/setup-runner-apt/action.yml"
_APT = re.compile(r"\b(?:apt-get|apt)\s")
_BOUND = re.compile(r"\btimeout --kill-after=5s 60s\s+apt(?:-get)?\s")
_OPTIONS = (
    "Acquire::Retries=0",
    "Acquire::http::Timeout=15",
    "Acquire::https::Timeout=15",
    "DPkg::Lock::Timeout=10",
    "DEBIAN_FRONTEND=noninteractive",
    "NEEDRESTART_MODE=a",
)
_BOUNDED = (
    "sudo -n env DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a "
    "timeout --kill-after=5s 60s apt-get -o Acquire::Retries=0 "
    "-o Acquire::http::Timeout=15 -o Acquire::https::Timeout=15 "
    "-o DPkg::Lock::Timeout=10 install -y bubblewrap"
)


def _violations(source: str, document: object) -> list[str]:
    if isinstance(document, list):
        return [item for node in document for item in _violations(source, node)]
    if not isinstance(document, dict):
        return []
    found: list[str] = []
    for key, value in document.items():
        if key != "run" or not isinstance(value, str):
            found.extend(_violations(source, value))
            continue
        found.extend(_script_violations(source, value))
    return found


def _script_violations(source: str, run: str) -> list[str]:
    found: list[str] = []
    for line in run.replace("\\\n", " ").splitlines():
        code = line.split("#", 1)[0].strip()
        if "playwright" in code and ("--with-deps" in code or "install-deps" in code):
            found.append(f"{source}: use runner system libraries instead of hidden apt: {code}")
        if _APT.search(code) and source != _SHARED_ACTION:
            found.append(f"{source}: use ./.github/actions/setup-runner-apt for runner apt: {code}")
            continue
        found.extend(
            f"{source}: bound apt with a 60s timeout and network/lock limits: {command}"
            for command in re.split(r"&&|\|\||[;|]", code)
            if _APT.search(command)
            and (not _BOUND.search(command) or any(option not in command for option in _OPTIONS))
        )
    return found


def test_every_workflow_and_setup_action_bounds_apt() -> None:
    paths = sorted(
        path
        for root in (REPO / ".github/workflows", REPO / ".github/actions")
        for path in root.rglob("*")
        if path.suffix in {".yml", ".yaml", ".sh"}
    )
    assert paths
    violations = [
        item
        for path in paths
        for item in (
            _script_violations(str(path.relative_to(REPO)), path.read_text())
            if path.suffix == ".sh"
            else _violations(str(path.relative_to(REPO)), yaml.safe_load(path.read_text()))
        )
    ]
    assert violations == []


@pytest.mark.parametrize("container", ["workflow", "action"])
@pytest.mark.parametrize(
    "command",
    [
        "sudo apt-get update",
        "sudo apt install -y bubblewrap",
        "pnpm exec playwright install --with-deps chromium",
        "pnpm exec playwright install chromium --with-deps",
        "pnpm exec playwright install-deps chromium",
        *[_BOUNDED.replace(option, "") for option in _OPTIONS],
        _BOUNDED.replace("timeout --kill-after=5s 60s ", ""),
        _BOUNDED.replace("60s", "600s"),
    ],
)
def test_unbounded_installs_are_reported(container: str, command: str) -> None:
    steps = [{"run": command}]
    document = (
        {"jobs": {"test": {"steps": steps}}}
        if container == "workflow"
        else {"runs": {"using": "composite", "steps": steps}}
    )
    violations = _violations(_SHARED_ACTION, document)
    assert len(violations) == 1
    assert _SHARED_ACTION in violations[0]
    assert command in violations[0]


@pytest.mark.parametrize(
    "command",
    [_BOUNDED, _BOUNDED.replace("apt-get", "apt"), "pnpm exec playwright install chromium"],
)
def test_bounded_installs_and_browser_only_downloads_are_allowed(command: str) -> None:
    assert _violations(_SHARED_ACTION, {"run": command}) == []


@pytest.mark.parametrize("separator", [";", "&&", "||", "|", "\n"])
def test_a_bounded_command_does_not_cover_a_later_apt_call(separator: str) -> None:
    violations = _violations(_SHARED_ACTION, {"run": f"{_BOUNDED} {separator} sudo apt-get update"})
    assert len(violations) == 1
    assert "sudo apt-get update" in violations[0]


@pytest.mark.parametrize("root", [".github/workflows", ".github/actions"])
@pytest.mark.parametrize(
    "command", [_BOUNDED, "sudo apt-get update", "sudo apt install bubblewrap"]
)
@example(component="new-action")
@given(component=st.from_regex(r"[a-z][a-z0-9_-]{0,30}", fullmatch=True))
def test_runner_apt_cannot_bypass_the_archive_cache(
    root: str, command: str, component: str
) -> None:
    for suffix in (".yml", ".yaml", "/install.sh"):
        source = f"{root}/{component}{suffix}"
        violations = _script_violations(source, command)
        assert len(violations) == 1
        assert source in violations[0]
        assert "use ./.github/actions/setup-runner-apt" in violations[0]


def test_archive_cache_hits_install_offline_and_misses_remain_bounded() -> None:
    action = yaml.safe_load((REPO / _SHARED_ACTION).read_text())
    steps = action["runs"]["steps"]
    downloads = [step for step in steps if _APT.search(step.get("run", ""))]
    assert downloads
    for step in downloads:
        assert "steps.cache.outputs.cache-hit != 'true'" in step["if"]
        assert _script_violations(_SHARED_ACTION, step["run"]) == []
        assert "--download-only" in step["run"]
    restore = next(step for step in steps if "actions/cache/restore@" in step.get("uses", ""))
    save = next(step for step in steps if "actions/cache/save@" in step.get("uses", ""))
    assert restore["with"]["key"] == save["with"]["key"]
    assert "restore-keys" not in restore["with"]
    assert restore["with"]["path"].endswith("/*.deb")
    assert restore["with"]["path"] == save["with"]["path"]
    prepare = next(step["run"] for step in steps if step.get("id") == "packages")
    for key_component in (
        "ImageOS",
        "ImageVersion",
        "dpkg --print-architecture",
        "package_hash",
        "sha256sum",
    ):
        assert key_component in prepare
    offline = next(
        step for step in steps if "dpkg --force-confold --install" in step.get("run", "")
    )
    assert "cache-hit" not in offline["if"]
    assert not _APT.search(offline["run"])
