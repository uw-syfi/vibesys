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
    assert 'NET_DEADLINE="${APT_UPDATE_DEADLINE}"' in update
    assert "retry apt-get update" in update
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
    assert "APT_UPDATE_DEADLINE=30" in block


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


def test_every_network_attempt_has_a_total_deadline_and_reports_its_result() -> None:
    """A per-request timeout cannot stop a stream that keeps making progress."""
    block = _split(SCRIPTS[0])[0]
    retry = block[block.index("retry() {") :]
    retry = retry[: retry.index("\n}")]
    assert 'timeout --kill-after=5 "${NET_DEADLINE}" "$@"' in retry
    assert "NET_ATTEMPTS=3" in block
    assert "NET_DEADLINE=120" in block
    assert retry.index("network start") < retry.index("if timeout")
    assert retry.index("network done") > retry.index("if timeout")
    assert "network failed (exit ${status})" in retry
    assert 'return "${status}"' in retry


@pytest.mark.parametrize(
    ("helper", "command"),
    [
        ("fetch", "curl"),
        ("apt_update_and_install", "apt-get update"),
        ("apt_update_and_install", "apt-get install"),
        ("npm_install_global", "npm install"),
        ("pip_install", "pip install"),
    ],
)
def test_package_fetches_use_the_logged_deadline(helper: str, command: str) -> None:
    block = _split(SCRIPTS[0])[0]
    function = block[block.index(f"{helper}() {{") :]
    function = function[: function.index("\n}")]
    assert f"retry {command}" in function, f"{helper}: wrap {command} in retry"


def test_agent_python_fetches_are_bounded_and_venv_creation_is_offline() -> None:
    agent = next(script for script in SCRIPTS if script.parent.name == "vibesys-agent")
    text = agent.read_text(encoding="utf-8")
    assert "UV_HTTP_CONNECT_TIMEOUT=20 UV_HTTP_TIMEOUT=30 UV_HTTP_RETRIES=1" in text
    assert 'retry uv python install "${PYTHON_VERSION}"' in text
    assert "retry uv pip install" in text
    assert "uv venv --offline" in text


def test_download_clients_bound_connection_waits_and_reuse_package_caches() -> None:
    block = _split(SCRIPTS[0])[0]
    for bound in (
        "--connect-timeout 20",
        '--max-time "${NET_DEADLINE}"',
        "Acquire::http::Timeout=30",
        "Acquire::https::Timeout=30",
        "--fetch-timeout=30000",
        "--timeout 30",
    ):
        assert bound in block
    assert "--prefer-offline --no-audit --no-fund" in block
    assert "--no-cache" not in block


def test_apt_retries_including_backoff_and_kill_grace_fit_three_minutes() -> None:
    block = _split(SCRIPTS[0])[0]
    constants = {
        name: int(value) for name, value in re.findall(r"^(APT_\w+)=(\d+)$", block, re.MULTILINE)
    }
    attempts = constants["APT_ATTEMPTS"]
    grace = 5
    backoff = sum(attempt * 5 for attempt in range(1, attempts))
    budget = sum(
        attempts * (constants[phase] + grace) + backoff
        for phase in ("APT_UPDATE_DEADLINE", "APT_INSTALL_DEADLINE")
    )
    assert attempts == 2
    assert "sleep $((attempt * 5))" in block
    assert budget <= 180
    update = block[block.index("apt_update_and_install() {") : block.index("apt_install() {")]
    assert update.count('NET_ATTEMPTS="${APT_ATTEMPTS}"') == 2
    assert 'NET_DEADLINE="${APT_INSTALL_DEADLINE}"' in update


def test_apt_skips_network_for_installed_packages_and_installs_only_missing_ones() -> None:
    block = _split(SCRIPTS[0])[0]
    install = block[block.index("apt_install() {") : block.index("npm_install_global() {")]
    assert "dpkg-query -W -f='${Status}'" in install
    assert "install ok installed" in install
    assert 'missing+=("${package}")' in install
    assert 'if [ "${#missing[@]}" -eq 0 ]; then' in install
    assert install.index("return 0") < install.index("apt_update_and_install")
    assert 'apt_update_and_install "${missing[@]}"' in install
