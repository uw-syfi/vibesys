"""Runner package acquisition uses the shared exact-image archive cache."""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest
import yaml
from hypothesis import example, given
from hypothesis import strategies as st

REPO = Path(__file__).resolve().parents[2]
_SHARED_ACTION = ".github/actions/setup-runner-apt/action.yml"
_APT = re.compile(r"\b(?:apt-get|apt)\s")
_BOUNDED = (
    "sudo -n env DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a "
    "timeout --kill-after=5s 60s apt-get -o Acquire::Retries=0 "
    "-o Acquire::http::Timeout=15 -o Acquire::https::Timeout=15 "
    "-o DPkg::Lock::Timeout=10 install -y bubblewrap"
)


def _script_violations(source: str, script: str) -> list[str]:
    if source == _SHARED_ACTION:
        return []
    violations: list[str] = []
    for line in script.replace("\\\n", " ").splitlines():
        if not _APT.search(line):
            continue
        lexer = shlex.shlex(line, posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        words: list[str] = []
        for word in [*lexer, ";"]:
            if not re.fullmatch(r"[;&|]+", word):
                words.append(word)
                continue
            literal_log = (
                words
                and words[0] in {"echo", "printf"}
                and not any("$(" in argument or "`" in argument for argument in words)
            )
            if not literal_log and _APT.search(" ".join(words)):
                violations.append(f"{source}: use ./.github/actions/setup-runner-apt: {line}")
            words = []
    return violations


def _violations(source: str, document: object) -> list[str]:
    if isinstance(document, list):
        return [item for node in document for item in _violations(source, node)]
    if not isinstance(document, dict):
        return []
    return [
        item
        for key, value in document.items()
        for item in (
            _script_violations(source, value)
            if key == "run" and isinstance(value, str)
            else _violations(source, value)
        )
    ]


def test_every_runner_apt_step_uses_the_shared_archive_cache() -> None:
    paths = [
        path
        for root in (REPO / ".github/workflows", REPO / ".github/actions")
        for path in root.rglob("*")
        if path.suffix in {".yml", ".yaml", ".sh"}
    ]
    assert paths
    assert [
        item
        for path in paths
        for item in (
            _script_violations(str(path.relative_to(REPO)), path.read_text())
            if path.suffix == ".sh"
            else _violations(str(path.relative_to(REPO)), yaml.safe_load(path.read_text()))
        )
    ] == []


@pytest.mark.parametrize("root", [".github/workflows", ".github/actions"])
@pytest.mark.parametrize(
    "command",
    [
        _BOUNDED,
        "sudo apt-get update",
        "sudo apt install bubblewrap",
        'bash -c "apt-get update"',
        'echo "$(apt-get update)"',
    ],
)
@pytest.mark.regression
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
    cached = next(step for step in steps if step.get("id") == "cached")
    assert "steps.cache.outputs.cache-hit == 'true'" in cached["if"]
    assert cached["continue-on-error"] is True
    assert not _APT.search(cached["run"])
    fallback = "steps.cached.outcome == 'failure'"
    for step in steps:
        if "dpkg --force-confold --install" in step.get("run", "") and step is not cached:
            assert fallback in step["if"]
    assert fallback in next(step for step in downloads)["if"]


def _caller_steps() -> list[tuple[str, dict[str, object]]]:
    callers = []
    for path in (REPO / ".github/workflows").glob("*.yml"):
        for job in yaml.safe_load(path.read_text())["jobs"].values():
            callers += [
                (path.name, step)
                for step in job.get("steps", [])
                if step.get("uses") == "./.github/actions/setup-runner-apt"
            ]
    return callers


def test_every_runner_apt_caller_bounds_the_step() -> None:
    # A composite step cannot carry its own timeout, so a slow cache restore or
    # a hung mirror is only bounded by the calling step.
    callers = _caller_steps()
    assert callers
    assert [name for name, step in callers if step.get("timeout-minutes") != 3] == []


@pytest.mark.parametrize("separator", [";", "&&", "||", "|", "|&", "&", "\n"])
def test_log_arguments_do_not_hide_runner_apt(separator: str) -> None:
    log = 'echo "Feature apt mirror: ${mirror}"'
    assert _script_violations("workflow.yml", log) == []
    assert len(_script_violations("workflow.yml", f"{log} {separator} sudo apt-get update")) == 1
