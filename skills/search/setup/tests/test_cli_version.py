"""The `setup` skill's version helper: how it classifies a vibesys-cli and picks a release.

The helper is a POSIX sh program, so its public surface is its command line. Each test runs it
under every POSIX shell found on the machine, against a copy of the skill whose minimum version
file the test writes, fake binaries that print what a real build prints, and a release listing
served from a file instead of the network. A Python model of caret ranges is the oracle.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

SKILL = Path(__file__).resolve().parents[1]
SCRIPT = SKILL / "scripts" / "cli_version.sh"
MIN_FILE = SKILL / "assets" / "min-cli-version"
SHELLS = [found for name in ("sh", "dash", "bash") if (found := shutil.which(name))]
CURL = shutil.which("curl")
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
    return subprocess.run(  # noqa: S603  # the helper is the system under test
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

TAG = st.one_of(
    st.builds(Version, SMALL, SMALL, SMALL, PRE).map(str),
    st.sampled_from(["foo", "1.2", "01.2.3"]),
)


def listing(folder: Path, tags: list[str], indent: int | None) -> str:
    refs = [{"ref": f"refs/tags/cli-v{tag}", "object": {"type": "tag"}} for tag in tags]
    path = folder / "refs.json"
    path.write_text(json.dumps(refs, indent=indent), encoding="utf-8")
    return path.as_uri()


def parsed(tag: str) -> Version | None:
    core, _, pre = tag.partition("-")
    parts = core.split(".")
    if len(parts) != 3 or not all(p.isdigit() and str(int(p)) == p for p in parts):
        return None
    return Version(*map(int, parts), pre)


@pytest.mark.skipif(CURL is None, reason="a file:// listing is read with curl")
@pytest.mark.parametrize("shell", SHELLS)
@given(
    minimum=MINIMUM,
    tags=st.lists(TAG, max_size=8),
    indent=st.sampled_from([None, 2]),
    compatible=st.booleans(),
)
def test_newest_picks_the_highest_stable_release(
    shell: str, minimum: Version, tags: list[str], indent: int | None, *, compatible: bool
) -> None:
    stable = [v for v in map(parsed, tags) if v is not None and not v.pre]
    if compatible:
        stable = [v for v in stable if expected_status(minimum, v) == "ok"]
    with tempfile.TemporaryDirectory() as root:
        script = skill_copy(Path(root), str(minimum))
        url = listing(Path(root), tags, indent)
        mode = "newest-compatible" if compatible else "newest"
        result = run(shell, script, mode, env={"VIBESYS_CLI_TAGS_URL": url})
    if not stable:
        assert result.returncode == 1
        assert result.stdout == ""
        return
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{max(stable, key=lambda v: v.core)}\n"


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("mode", ["newest", "newest-compatible"])
def test_an_unreachable_listing_fails(shell: str, mode: str, tmp_path: Path) -> None:
    url = (tmp_path / "absent.json").as_uri()
    result = run(shell, SCRIPT, mode, env={"VIBESYS_CLI_TAGS_URL": url})
    assert result.returncode == 1
    assert result.stdout == ""
