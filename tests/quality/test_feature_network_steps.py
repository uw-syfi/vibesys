"""Every network step in a VibeSys Feature install script is bounded and retried.

A stalled fetch inside ``docker build`` blocks the whole image build until the
caller's budget expires, so each Feature routes its fetches through one set of
helpers that set deadlines and a bounded retry count. Features cannot share
files, so the helper block is duplicated in every ``install.sh``; this test
keeps the copies identical and keeps raw network commands out of the rest of
each script.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
START = "# >>> vibesys network helpers"
END = "# <<< vibesys network helpers"
SCRIPTS = sorted(REPO_ROOT.glob("libs/*/src/*/**/features/src/*/install.sh"))
#: Commands that touch the network when run bare. ``uv venv`` and ``usermod`` do not.
_RAW_NETWORK_COMMAND = re.compile(
    r"\b(?:curl|wget|apt-get|npm install|pip3? install|uv python install|uv pip install)\b"
    r"|\bsh \S*rustup-init\.sh\b"
)
_NPM_BOUNDS = (
    "--fetch-retries=",
    "--fetch-timeout=",
    "--fetch-retry-mintimeout=",
    "--fetch-retry-maxtimeout=",
)


def _split(script: Path) -> tuple[str, list[str]]:
    """Return the helper block and the script's lines outside it."""
    lines = script.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(START))
    end = next(i for i, line in enumerate(lines) if line.startswith(END))
    return "\n".join(lines[start : end + 1]), lines[:start] + lines[end + 1 :]


def test_every_feature_script_is_found() -> None:
    assert {path.parent.name for path in SCRIPTS} >= {
        "vibesys-agent",
        "vibesys-container-runtime",
        "vibesys-go",
        "vibesys-pip-extras",
        "vibesys-rust",
    }


def test_the_helper_block_is_identical_in_every_feature() -> None:
    blocks = {script: _split(script)[0] for script in SCRIPTS}
    assert len(set(blocks.values())) == 1, "network helper copies drifted apart"


def test_npm_runs_with_bounded_fetch_settings() -> None:
    block = _split(SCRIPTS[0])[0]
    function = block[block.index("npm_install_global() {") :]
    function = function[: function.index("\n}")]
    for option in _NPM_BOUNDS:
        assert option in function
    assert "retry npm install" in function


def test_apt_update_drops_gpu_vendor_indexes_and_is_deadline_bounded() -> None:
    """The CUDA and ROCm bases publish package indexes that transfer for minutes.

    Updates with ``Acquire::http::Timeout=30`` still ran for several minutes,
    so the helper removes those lists and caps the update.
    """
    block = _split(SCRIPTS[0])[0]
    update = block[block.index("apt_update_and_install() {") :]
    update = update[: update.index("\n}")]
    assert "drop_vendor_apt_sources" in update
    assert 'timeout "${APT_UPDATE_DEADLINE}" apt-get update' in update
    dropped = block[block.index("drop_vendor_apt_sources() {") :]
    dropped = dropped[: dropped.index("\n}")]
    for marker in (
        "*nvidia*",
        "*cuda*",
        "*rocm*",
        "*amdgpu*",
        "nvidia\\.com",
        "repo\\.radeon\\.com",
    ):
        assert marker in dropped
    assert "APT_UPDATE_DEADLINE=60" in block


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda path: path.parent.name)
def test_no_network_command_runs_outside_the_helpers(script: Path) -> None:
    _block, rest = _split(script)
    for line in rest:
        code = line.strip()
        if code.startswith("#") or _RAW_NETWORK_COMMAND.search(code) is None:
            continue
        assert re.match(
            r"^(?:\w+=\S+ )*(?:retry|fetch|apt_install|pip_install|npm_install_global)\b", code
        ), f"{script.parent.name}: unbounded network command: {code}"
