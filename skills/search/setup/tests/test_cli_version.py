"""The `setup` skill's version helper: how it classifies a vibesys-cli and picks a release.

The helper is a POSIX sh program, so its public surface is its command line. Each test runs it
under every POSIX shell found on the machine, against a copy of the skill whose minimum version
file the test writes, fake binaries that print what a real build prints, and a local HTTP
server standing in for GitHub's tag listing and release downloads. A Python model of caret ranges is the oracle.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

if TYPE_CHECKING:
    from collections.abc import Iterator

SKILL = Path(__file__).resolve().parents[1]
SCRIPT = SKILL / "scripts" / "cli_version.sh"
MIN_FILE = SKILL / "assets" / "min-cli-version"
SHELLS = [found for name in ("sh", "dash", "bash") if (found := shutil.which(name))]
EXIT = {"ok": 0, "too-old": 3, "too-new": 4, "dev": 5, "unknown": 6}


@dataclass(frozen=True)
class Version:
    major: int
    minor: int
    patch: int
    pre: str = ""

    def __str__(self) -> str:
        core = f"{self.major}.{self.minor}.{self.patch}"
        return f"{core}-{self.pre}" if self.pre else core

    @property
    def core(self) -> tuple[int, int, int]:
        return (self.major, self.minor, self.patch)


def expected_status(minimum: Version, version: Version) -> str:
    """Caret semantics on ``minimum``; a pre-release is below its release, and a pre-release
    of a version past the range is past it too."""
    if version.core < minimum.core or (version.core == minimum.core and version.pre):
        return "too-old"
    if minimum.major > 0:
        compatible = version.major == minimum.major
    elif minimum.minor > 0:
        compatible = version.major == 0 and version.minor == minimum.minor
    else:
        compatible = version.core == minimum.core
    return "ok" if compatible else "too-new"


# --- running the helper ------------------------------------------------------------------------


def skill_copy(root: Path, minimum: str) -> Path:
    """A copy of the skill's script beside a minimum version file holding ``minimum``."""
    (root / "scripts").mkdir()
    (root / "assets").mkdir()
    shutil.copy(SCRIPT, root / "scripts" / SCRIPT.name)
    (root / "assets" / MIN_FILE.name).write_text(f"{minimum}\n", encoding="utf-8")
    return root / "scripts" / SCRIPT.name


def run(
    shell: str, script: Path, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    environment = {"PATH": os.environ.get("PATH", ""), **(env or {})}
    return subprocess.run(  # noqa: S603  # LW-100377; the helper script is the system under test.
        [shell, str(script), *args],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
        timeout=60,  # guards against a hang only
    )


def fake_binary(folder: Path, reported: str, *, json_flag: bool) -> Path:
    """A stand-in vibesys-cli printing ``reported`` as a release build prints its version."""
    path = folder / "vibesys-cli"
    lines = ["#!/bin/sh"]
    if json_flag:
        lines.append(
            'if [ "$1" = --version ] && [ "${2:-}" = --json ]; then printf \'%s\\n\' '
            f'\'{{"binary_version":"{reported}","protocol_version":3}}\'; exit 0; fi'
        )
    lines += [
        'if [ "$#" -eq 1 ] && [ "$1" = --version ]; then',
        f"  printf '%s\\n' 'vibesys-cli {reported} (task schemas 0.1, 0.2)'; exit 0",
        "fi",
        "printf 'unknown flag\\n' >&2; exit 2",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o755)
    return path


# --- strategies --------------------------------------------------------------------------------

SMALL = st.integers(0, 4)
COMPONENT = st.one_of(SMALL, st.integers(0, 10**25))
PRE = st.one_of(
    st.just(""),
    st.integers(0, 12).map(lambda n: f"rc.{n}"),
    st.from_regex(r"[0-9A-Za-z]{1,4}(\.[0-9A-Za-z]{1,4}){0,2}", fullmatch=True),
)
MINIMUM = st.builds(Version, SMALL, SMALL, SMALL)


@st.composite
def near(draw: st.DrawFn, minimum: Version) -> Version:
    """A version whose components sit on, next to, or far from the minimum's."""

    def component(value: int) -> int:
        return draw(st.one_of(st.sampled_from([value, value + 1, max(value - 1, 0)]), COMPONENT))

    return Version(
        component(minimum.major), component(minimum.minor), component(minimum.patch), draw(PRE)
    )


@st.composite
def cases(draw: st.DrawFn) -> tuple[Version, Version]:
    minimum = draw(MINIMUM)
    return minimum, draw(near(minimum))


# --- classification ----------------------------------------------------------------------------


@pytest.mark.parametrize("shell", SHELLS)
@given(case=cases())
# The edges of each caret shape: a pre-release of the minimum, the next breaking version and its
# pre-release, and a larger component that only a numeric (not textual) comparison orders.
@example(case=(Version(0, 1, 0), Version(0, 1, 0, "rc.3")))
@example(case=(Version(0, 1, 0), Version(0, 2, 0, "rc.1")))
@example(case=(Version(0, 1, 0), Version(0, 1, 10)))
@example(case=(Version(1, 2, 3), Version(1, 10, 0)))
@example(case=(Version(1, 2, 3), Version(2, 0, 0)))
@example(case=(Version(0, 0, 3), Version(0, 0, 4)))
def test_classify_follows_caret_semantics(shell: str, case: tuple[Version, Version]) -> None:
    minimum, version = case
    with tempfile.TemporaryDirectory() as root:
        script = skill_copy(Path(root), str(minimum))
        result = run(shell, script, "classify", str(version))
    status = expected_status(minimum, version)
    assert result.returncode == EXIT[status], result.stdout + result.stderr
    assert result.stdout.startswith(f"{status}: ")


VERSION_GRAMMAR = re.compile(
    r"v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-[0-9A-Za-z.-]+)?"
)
"""MAJOR.MINOR.PATCH[-PRERELEASE] as release tags allow it, with an optional leading ``v``."""


@pytest.mark.parametrize("shell", SHELLS)
@given(
    text=st.one_of(
        st.text(alphabet="0123456789.-v+rc \n", max_size=12),
        st.sampled_from(["01.2.3", "1.2", "1.2.3.4", "1.2.3+build", "1.2.3-", "0.1.0\n", ""]),
    )
)
def test_only_a_version_is_classified(shell: str, text: str) -> None:
    result = run(shell, SCRIPT, "classify", text)
    if VERSION_GRAMMAR.fullmatch(text):
        assert result.returncode in {EXIT["ok"], EXIT["too-old"], EXIT["too-new"]}
    else:
        assert result.returncode == EXIT["unknown"], result.stdout
        assert result.stdout.startswith("unknown: ")


@pytest.mark.parametrize("shell", SHELLS)
def test_dev_build_is_its_own_status(shell: str) -> None:
    result = run(shell, SCRIPT, "classify", "dev")
    assert (result.returncode, result.stdout.split(":")[0]) == (EXIT["dev"], "dev")


@pytest.mark.parametrize("shell", SHELLS)
def test_the_shipped_minimum_is_supported_and_its_release_candidates_are_not(shell: str) -> None:
    minimum = MIN_FILE.read_text(encoding="utf-8").strip()
    assert run(shell, SCRIPT, "classify", minimum).returncode == EXIT["ok"]
    assert run(shell, SCRIPT, "classify", f"{minimum}-rc.1").returncode == EXIT["too-old"]


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("bad", ["", "0.1", "0.1.0-rc.1", "latest", "0.1.0\n0.2.0"])
def test_an_unusable_minimum_file_fails_and_names_the_file(
    shell: str, bad: str, tmp_path: Path
) -> None:
    script = skill_copy(tmp_path, bad)
    result = run(shell, script, "classify", "0.1.0")
    assert result.returncode == 1
    assert MIN_FILE.name in result.stderr


# --- checking a binary -------------------------------------------------------------------------


@pytest.mark.parametrize("shell", SHELLS)
@given(case=cases(), json_flag=st.booleans(), via_env=st.booleans())
def test_check_reads_the_binary_version_either_way(
    shell: str, case: tuple[Version, Version], *, json_flag: bool, via_env: bool
) -> None:
    minimum, version = case
    with tempfile.TemporaryDirectory() as root:
        script = skill_copy(Path(root), str(minimum))
        binary = fake_binary(Path(root), str(version), json_flag=json_flag)
        if via_env:
            result = run(shell, script, "check", env={"VIBESYS_CLI": str(binary)})
        else:
            result = run(shell, script, "check", str(binary))
    status = expected_status(minimum, version)
    assert result.returncode == EXIT[status], result.stdout + result.stderr
    assert result.stdout.startswith(f"{status}: {version} ")


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("style", ["json", "plain"])
def test_check_reports_a_development_build(shell: str, style: str, tmp_path: Path) -> None:
    binary = fake_binary(tmp_path, "dev", json_flag=style == "json")
    assert run(shell, SCRIPT, "check", str(binary)).returncode == EXIT["dev"]


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize(
    "body",
    ["exit 1", "printf 'hello\\n'", "printf '{\"protocol_version\":3}\\n'"],
    ids=["fails", "no-version", "json-without-version"],
)
def test_check_of_a_binary_without_a_readable_version_is_unknown(
    shell: str, body: str, tmp_path: Path
) -> None:
    binary = tmp_path / "vibesys-cli"
    binary.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    binary.chmod(0o755)
    result = run(shell, SCRIPT, "check", str(binary))
    assert result.returncode == EXIT["unknown"]
    assert str(binary) in result.stdout


@pytest.mark.parametrize("shell", SHELLS)
def test_check_of_a_missing_binary_is_unknown(shell: str, tmp_path: Path) -> None:
    result = run(shell, SCRIPT, "check", str(tmp_path / "absent"))
    assert result.returncode == EXIT["unknown"]


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize(
    "args", [[], ["check"], ["classify"], ["newest", "x"], ["upgrade"], ["check", "a", "b"]]
)
def test_usage_errors_exit_two(shell: str, args: list[str]) -> None:
    assert run(shell, SCRIPT, *args).returncode == 2


# --- picking a release -------------------------------------------------------------------------
#
# A local HTTP server stands in for GitHub: the tag listing and the release assets are routes, and
# any other path answers 404, as a tag whose release workflow failed does. curl's file:// support
# cannot tell a missing file from a server error, so the fake speaks HTTP.


class FakeGitHub:
    """Routes from path to (status, body); every other path is a 404."""

    def __init__(self) -> None:
        self.routes: dict[str, tuple[int, bytes]] = {}
        routes = self.routes

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                status, body = routes.get(self.path, (404, b"Not Found"))
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_: object) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def env(self) -> dict[str, str]:
        return {
            "VIBESYS_CLI_TAGS_URL": f"{self.url}/tags",
            "VIBESYS_CLI_RELEASES_URL": f"{self.url}/download/",
        }

    def publish(self, tag: str, status: int = 200) -> None:
        self.routes[f"/download/{tag}/checksums.txt"] = (status, b"0" * 64 + b"  x\n")

    def list_tags(self, tags: list[str], indent: int | None = None) -> None:
        refs = [{"ref": f"refs/tags/cli-v{tag}", "object": {"type": "tag"}} for tag in tags]
        self.routes["/tags"] = (200, json.dumps(refs, indent=indent).encode())

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=60)  # guards against a hang only


@pytest.fixture(scope="module")
def github() -> Iterator[FakeGitHub]:
    fake = FakeGitHub()
    yield fake
    fake.close()


TAG = st.one_of(
    st.builds(Version, SMALL, SMALL, SMALL, PRE).map(str),
    st.sampled_from(["foo", "1.2", "01.2.3"]),
)


def parsed(tag: str) -> Version | None:
    core, _, pre = tag.partition("-")
    parts = core.split(".")
    if len(parts) != 3 or not all(p.isdigit() and str(int(p)) == p for p in parts):
        return None
    return Version(*map(int, parts), pre)


def run_against(
    github: FakeGitHub, shell: str, minimum: Version, mode: str
) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory() as root:
        script = skill_copy(Path(root), str(minimum))
        return run(shell, script, mode, env=github.env())


needs_a_fetcher = pytest.mark.skipif(
    not (shutil.which("curl") or shutil.which("wget")), reason="the lookups need curl or wget"
)


@needs_a_fetcher
@pytest.mark.parametrize("shell", SHELLS)
@given(
    minimum=MINIMUM,
    tags=st.lists(st.tuples(TAG, st.booleans()), max_size=8),
    indent=st.sampled_from([None, 2]),
)
@example(minimum=Version(0, 1, 0), tags=[("0.1.0", False), ("0.1.0-rc.2", True)], indent=2)
# Two supported tags whose releases both failed: once looped forever, because parse overwrote the
# list of candidates still to probe.
@example(minimum=Version(3, 0, 0), tags=[("3.3.0", False), ("3.3.3", False)], indent=None)
def test_newest_compatible_is_the_highest_supported_release_that_is_published(
    github: FakeGitHub,
    shell: str,
    minimum: Version,
    tags: list[tuple[str, bool]],
    indent: int | None,
) -> None:
    github.routes.clear()
    github.list_tags([tag for tag, _ in tags], indent)
    for tag, published in tags:
        if published:
            github.publish(f"cli-v{tag}")
    published_tags = {tag for tag, published in tags if published}
    supported = [
        v
        for v in map(parsed, published_tags)
        if v is not None and not v.pre and expected_status(minimum, v) == "ok"
    ]
    result = run_against(github, shell, minimum, "newest-compatible")
    if not supported:
        assert (result.returncode, result.stdout) == (1, ""), result.stderr
        assert "no published stable release" in result.stderr
        return
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{max(supported, key=lambda v: v.core)}\n"


@needs_a_fetcher
@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("status", [500, 403])
def test_a_server_error_on_a_candidate_fails_instead_of_skipping_it(
    github: FakeGitHub, shell: str, status: int
) -> None:
    github.routes.clear()
    github.list_tags(["0.1.0", "0.1.1"])
    github.publish("cli-v0.1.0")
    github.publish("cli-v0.1.1", status)
    result = run_against(github, shell, Version(0, 1, 0), "newest-compatible")
    assert (result.returncode, result.stdout) == (1, "")
    assert f"HTTP {status}" in result.stderr


@needs_a_fetcher
@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("status", [404, 500])
def test_a_tag_listing_that_does_not_answer_fails(
    github: FakeGitHub, shell: str, status: int
) -> None:
    github.routes.clear()
    github.routes["/tags"] = (status, b"")
    github.publish("cli-v0.1.0")
    result = run_against(github, shell, Version(0, 1, 0), "newest-compatible")
    assert (result.returncode, result.stdout) == (1, "")


@needs_a_fetcher
@pytest.mark.parametrize("shell", SHELLS)
@given(version=st.builds(Version, COMPONENT, COMPONENT, COMPONENT).map(str), trailer=st.booleans())
def test_newest_is_the_version_cli_latest_serves(
    github: FakeGitHub, shell: str, version: str, *, trailer: bool
) -> None:
    github.routes.clear()
    body = f"{version}\n" if trailer else version
    github.routes["/download/cli-latest/VERSION"] = (200, body.encode())
    result = run_against(github, shell, Version(0, 1, 0), "newest")
    assert (result.returncode, result.stdout) == (0, f"{version}\n"), result.stderr


@needs_a_fetcher
@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize(
    ("status", "body", "message"),
    [
        (404, b"", "no stable release is published"),
        (500, b"", "HTTP 500"),
        (200, b"0.2.0-rc.1\n", "not a stable version"),
        (200, b"<html>", "not a stable version"),
    ],
    ids=["missing", "server-error", "pre-release", "garbage"],
)
def test_newest_without_a_stable_cli_latest_fails(
    github: FakeGitHub, shell: str, status: int, body: bytes, message: str
) -> None:
    github.routes.clear()
    github.routes["/download/cli-latest/VERSION"] = (status, body)
    result = run_against(github, shell, Version(0, 1, 0), "newest")
    assert (result.returncode, result.stdout) == (1, "")
    assert message in result.stderr


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("mode", ["newest", "newest-compatible"])
def test_an_unreachable_server_fails(shell: str, mode: str) -> None:
    closed = FakeGitHub()
    env = closed.env()
    closed.close()  # nothing listens on its port any more
    result = run(shell, SCRIPT, mode, env=env)
    assert (result.returncode, result.stdout) == (1, "")
