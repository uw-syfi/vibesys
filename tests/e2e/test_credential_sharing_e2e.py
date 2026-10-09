"""A login refreshed inside a real agent container reaches the host credential file.

Skipped unless ``VIBESYS_E2E_DOCKER=1`` and ``docker`` is on PATH, so an
ordinary ``pytest`` run needs neither Docker nor the agent image:

```bash
VIBESYS_E2E_DOCKER=1 uv run pytest tests/e2e/test_credential_sharing_e2e.py -q -s
```

Every file here lives in a temporary directory standing in for the host home;
no real credential is read, mounted, or written.

A CLI saves a refreshed token one of two ways, and both are exercised: it
rewrites the file in place, or (Claude Code) stages a temporary file and
renames it over the path, then rewrites in place when the rename is refused.
A file mounted from the host cannot be replaced by a rename, so the second
style reaches the host through its in-place fallback; a writer that only
renames fails closed, leaving the host file intact.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

import agentshim
import pytest

from vs_agent.api import auth_bind_mounts, auth_copy_paths
from vs_agent.api.images import agent_image
from vs_sandbox.docker_sandbox import DockerSandbox

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.e2e

_ENABLE_ENV = "VIBESYS_E2E_DOCKER"
#: Overridable so a host whose Docker store lacks the default base can use
#: another Debian-derived image.
_BASE_IMAGE = os.environ.get("VIBESYS_E2E_DOCKER_BASE_IMAGE", "python:3.12-bookworm")
_BUILD_TIMEOUT_S = 900.0
_PROVIDER = "claude"


@pytest.fixture
def host_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway home holding the provider's credential and settings files."""
    profile = agentshim.get_provider(_PROVIDER).profile
    home = tmp_path / "home"
    for relative in profile.auth_files:
        (home / relative).parent.mkdir(parents=True, exist_ok=True)
        (home / relative).write_text("host-original\n")
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture
def container(tmp_path: Path, host_home: Path) -> Iterator[DockerSandbox]:
    del host_home
    if os.environ.get(_ENABLE_ENV) != "1" or shutil.which("docker") is None:
        pytest.skip(f"set {_ENABLE_ENV}=1 with docker on PATH to run against a real daemon")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sandbox = DockerSandbox(
        host_workspace=str(workspace),
        image=agent_image(_BASE_IMAGE, timeout=_BUILD_TIMEOUT_S),
        bind_mounts=auth_bind_mounts(_PROVIDER),
        auth_files=auth_copy_paths(_PROVIDER),
    )
    sandbox.start()
    try:
        yield sandbox
    finally:
        sandbox.stop()


def _credential(home: Path) -> Path:
    return home / agentshim.get_provider(_PROVIDER).profile.credential_files[0]


def _settings(home: Path) -> list[Path]:
    profile = agentshim.get_provider(_PROVIDER).profile
    return [home / f for f in profile.auth_files if f not in profile.credential_files]


def _in_container(relative: str) -> str:
    return f"$HOME/{relative}"


def test_an_in_place_write_reaches_the_host_file(container: DockerSandbox, host_home: Path) -> None:
    target = _in_container(agentshim.get_provider(_PROVIDER).profile.credential_files[0])

    result = container.execute(f"printf in-place > {target}")

    assert result.exit_code == 0, result.output
    assert _credential(host_home).read_text() == "in-place"


def test_a_temp_then_rename_write_reaches_the_host_file(
    container: DockerSandbox, host_home: Path
) -> None:
    target = _in_container(agentshim.get_provider(_PROVIDER).profile.credential_files[0])

    # Claude Code's order: stage next to the target, rename over it, and when
    # the rename is refused (EBUSY on a mounted file) rewrite in place.
    result = container.execute(
        f"printf renamed > {target}.tmp && "
        f"{{ mv {target}.tmp {target} 2>/dev/null || {{ cat {target}.tmp > {target}; "
        f"rm {target}.tmp; }}; }}"
    )

    assert result.exit_code == 0, result.output
    assert _credential(host_home).read_text() == "renamed"


def test_a_rename_only_writer_fails_closed_and_leaves_the_host_file_intact(
    container: DockerSandbox, host_home: Path
) -> None:
    target = _in_container(agentshim.get_provider(_PROVIDER).profile.credential_files[0])

    result = container.execute(f"printf renamed > {target}.tmp && mv {target}.tmp {target}")

    assert result.exit_code != 0
    assert _credential(host_home).read_text() == "host-original\n"


def test_settings_and_history_stay_inside_the_container(
    container: DockerSandbox, host_home: Path
) -> None:
    profile = agentshim.get_provider(_PROVIDER).profile
    setting = next(f for f in profile.auth_files if f not in profile.credential_files)
    credential_dir = Path(profile.credential_files[0]).parent

    result = container.execute(
        f"printf container > {_in_container(setting)} && "
        f"printf container > {_in_container(str(credential_dir))}/sessions-in-container.jsonl"
    )

    assert result.exit_code == 0, result.output
    # The setting is the container's own copy, and a new file next to the
    # shared credential never lands on the host.
    for path in _settings(host_home):
        assert path.read_text() == "host-original\n"
    assert not (_credential(host_home).parent / "sessions-in-container.jsonl").exists()
    # The shared credential was not rewritten by any of that.
    assert _credential(host_home).read_text() == "host-original\n"
