"""Client CI downloads stay cached, retried, and off the broken apt source.

The setup-clients action is the only place a client job prepares Node, pnpm,
Electron, and Playwright. These tests hold that contract: the Electron binary
and the pnpm binary are cached by version, download steps retry through one
helper, and ``playwright install --with-deps`` cannot run until the
``packages.microsoft.com`` apt source is removed.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from hypothesis import given
from hypothesis import strategies as st
from scripts.client_ci_downloads import (
    EXIT_LOCKFILE,
    EXIT_OK,
    PLAYWRIGHT_WITH_DEPS,
    electron_version,
    main,
    violations_in_document,
)

REPO = Path(__file__).resolve().parents[2]
SETUP_ACTION = REPO / ".github" / "actions" / "setup-clients" / "action.yml"
RETRY_HELPER = REPO / ".github" / "actions" / "setup-clients" / "retry-download.sh"
LOCKFILE = REPO / "clients" / "pnpm-lock.yaml"
_FILL = st.text(alphabet="abcxyz012", max_size=24)
_GUARD = (
    "grep -l packages.microsoft.com /etc/apt/sources.list.d\n"
    "sudo rm -f /etc/apt/sources.list.d/microsoft.list\n"
)
_COMMAND = "pnpm exec playwright install --with-deps chromium\n"
_DOWNLOADS = (
    "corepack install --global",
    "packaging/fetch_bun.py",
    "uv python install",
    "uv sync --dev",
    "pnpm install --frozen-lockfile",
    PLAYWRIGHT_WITH_DEPS,
)


def _load(path: Path) -> object:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _mapping(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    mapped: dict[str, object] = {}
    for key, item in value.items():
        assert isinstance(key, str)
        mapped[key] = item
    return mapped


def _ci_documents() -> list[tuple[str, object]]:
    roots = (REPO / ".github" / "workflows", REPO / ".github" / "actions")
    paths = sorted(
        path
        for root in roots
        for path in root.rglob("*")
        if path.suffix in {".yml", ".yaml"} and path.is_file()
    )
    return [(str(path.relative_to(REPO)), _load(path)) for path in paths]


def _steps(document: object) -> list[dict[str, object]]:
    runs = _mapping(_mapping(document)["runs"])
    steps = runs["steps"]
    assert isinstance(steps, list)
    return [_mapping(step) for step in steps]


def _cache_named(action: object, prefix: str) -> dict[str, object]:
    matches: list[dict[str, object]] = []
    for step in _steps(action):
        uses = step.get("uses", "")
        if not isinstance(uses, str) or not uses.startswith("actions/cache@"):
            continue
        key = _mapping(step["with"]).get("key")
        if isinstance(key, str) and key.startswith(prefix):
            matches.append(step)
    assert len(matches) == 1, prefix
    return matches[0]


def _with_field(step: dict[str, object], field: str) -> str:
    value = _mapping(step["with"])[field]
    assert isinstance(value, str)
    return value


def test_electron_version_reads_the_lockfile_pin() -> None:
    assert electron_version(LOCKFILE.read_text(encoding="utf-8")) == "44.6.0"


def test_electron_version_rejects_zero_or_many(tmp_path: Path) -> None:
    empty = tmp_path / "empty.yaml"
    empty.write_text("packages: {}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="none"):
        electron_version(empty.read_text(encoding="utf-8"))

    both = "  electron@1.2.3:\n  electron@4.5.6:\n"
    with pytest.raises(ValueError, match=r"1\.2\.3") as caught:
        electron_version(both)
    assert "4.5.6" in str(caught.value)


def test_cli_prints_the_lockfile_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([str(LOCKFILE)]) == EXIT_OK
    assert capsys.readouterr().out.strip() == "44.6.0"


def test_cli_reports_an_unreadable_lockfile(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = tmp_path / "lock"
    directory.mkdir()
    assert main([str(directory)]) == EXIT_LOCKFILE
    assert "cannot read" in capsys.readouterr().err


def test_setup_action_caches_electron_and_pnpm_by_version() -> None:
    action = _load(SETUP_ACTION)
    electron = _cache_named(action, "electron-")
    assert _with_field(electron, "path") == "${{ env.ELECTRON_CACHE }}"
    assert "steps.electron.outputs.version" in _with_field(electron, "key")
    pnpm_binary = _cache_named(action, "pnpm-binary-")
    assert _with_field(pnpm_binary, "path") == "${{ env.COREPACK_HOME }}"
    assert "env.PNPM_VERSION" in _with_field(pnpm_binary, "key")
    store = _cache_named(action, "pnpm-store-")
    assert _with_field(store, "path") == "${{ env.PNPM_STORE_PATH }}"
    browsers = _cache_named(action, "playwright-chromium-")
    assert _with_field(browsers, "path") == "~/.cache/ms-playwright"
    assert "steps.playwright.outputs.version" in _with_field(browsers, "key")
    text = SETUP_ACTION.read_text(encoding="utf-8")
    assert text.count("PNPM_VERSION=11.11.0") == 1
    assert "scripts/client_ci_downloads.py" in text


def test_download_steps_use_the_three_attempt_helper() -> None:
    helper = RETRY_HELPER.read_text(encoding="utf-8")
    assert re.search(r"(?m)^attempts=3$", helper)
    runs = [run for step in _steps(_load(SETUP_ACTION)) if isinstance(run := step.get("run"), str)]
    for command in _DOWNLOADS:
        matches = [run for run in runs if command in run]
        assert len(matches) == 1, command
        assert "retry-download.sh" in matches[0]
        assert matches[0].index("retry-download.sh") < matches[0].index(command)


def test_workflows_do_not_install_playwright_with_deps() -> None:
    hits = [
        str(path.relative_to(REPO))
        for path in (REPO / ".github" / "workflows").glob("*")
        if path.is_file() and PLAYWRIGHT_WITH_DEPS in path.read_text(encoding="utf-8")
    ]
    assert hits == []


def test_only_the_e2e_job_asks_setup_clients_for_playwright() -> None:
    # PyYAML reads the workflow's `on` key as a boolean, so the root mapping
    # is not string-keyed. Job names are.
    loaded = _load(REPO / ".github" / "workflows" / "test.yml")
    assert isinstance(loaded, dict)
    seen = False
    for name, job in _mapping(loaded["jobs"]).items():
        steps = _mapping(job).get("steps") or []
        assert isinstance(steps, list)
        for step in steps:
            body = _mapping(step)
            uses = body.get("uses", "")
            if not isinstance(uses, str) or not uses.endswith("setup-clients"):
                continue
            with_block = body.get("with")
            playwright = (
                _mapping(with_block).get("playwright") if isinstance(with_block, dict) else None
            )
            if name == "clients-e2e":
                assert playwright is True
                seen = True
            else:
                assert playwright in (None, False)
    assert seen


def test_checked_in_ci_removes_the_microsoft_apt_source_before_playwright() -> None:
    violations = [
        item
        for source, document in _ci_documents()
        for item in violations_in_document(source, document)
    ]
    assert violations == []


def test_a_job_without_the_guard_is_named_in_the_violation() -> None:
    document = {
        "jobs": {
            "clients-e2e": {
                "steps": [
                    {"name": "Install Chromium", "run": "playwright install --with-deps chromium"},
                ]
            }
        }
    }
    violations = violations_in_document(".github/workflows/test.yml", document)
    assert len(violations) == 1
    assert "clients-e2e" in violations[0]
    assert "Install Chromium" in violations[0]
    assert "packages.microsoft.com" in violations[0]


def test_a_guard_in_an_earlier_step_covers_the_install() -> None:
    document = {
        "runs": {
            "using": "composite",
            "steps": [
                {
                    "name": "drop",
                    "run": _GUARD,
                },
                {"name": "install", "run": _COMMAND},
            ],
        }
    }
    assert violations_in_document(".github/actions/setup-clients/action.yml", document) == []


def test_documents_without_jobs_or_composite_steps_have_no_violations() -> None:
    assert violations_in_document("notes.md", None) == []
    assert violations_in_document("notes.md", {"on": "push"}) == []


@given(fill=_FILL, place=st.sampled_from(["before", "after", "missing"]))
def test_with_deps_is_guarded_only_when_the_source_is_removed_first(fill: str, place: str) -> None:
    if place == "before":
        run = f"{fill}\n{_GUARD}{_COMMAND}"
    elif place == "after":
        run = f"{fill}\n{_COMMAND}{_GUARD}"
    else:
        run = f"{fill}\n{_COMMAND}"
    violations = violations_in_document(
        "job", {"runs": {"using": "composite", "steps": [{"run": run}]}}
    )
    assert (violations == []) is (place == "before")
