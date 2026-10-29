"""Client CI downloads stay cached, retried, and off the broken apt source.

The setup-clients action is the only place a client job prepares Node, pnpm,
Electron, and Playwright. These tests hold that contract: the Electron binary
and the pnpm binary are cached by version, download steps retry through one
helper. Chromium uses the runner system libraries without apt.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from scripts.client_ci_downloads import (
    EXIT_LOCKFILE,
    EXIT_OK,
    electron_version,
    main,
)

REPO = Path(__file__).resolve().parents[2]
SETUP_ACTION = REPO / ".github" / "actions" / "setup-clients" / "action.yml"
RETRY_HELPER = REPO / ".github" / "actions" / "setup-clients" / "retry-download.sh"
LOCKFILE = REPO / "clients" / "pnpm-lock.yaml"
_DOWNLOADS = (
    "corepack install --global",
    "packaging/fetch_bun.py",
    "uv python install",
    "uv sync --dev",
    "pnpm install --frozen-lockfile",
    "playwright install chromium",
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


def test_browser_download_attempts_are_bounded() -> None:
    runs = [step.get("run", "") for step in _steps(_load(SETUP_ACTION))]
    install = next(
        run for run in runs if isinstance(run, str) and "playwright install chromium" in run
    )
    assert "timeout --kill-after=5s 60s pnpm" in install
