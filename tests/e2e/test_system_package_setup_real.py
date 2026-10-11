"""Docker-target evaluator setup against a real shell and Fake package-manager executables.

What only a real shell shows: that the generated root setup installs exactly the system packages
a declared tool needs, and leaves the package manager alone when they are already present.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from vs_runtime.api.infrastructure import TrustedEvaluatorRequirements, evaluator_container_setup
from vs_sandbox.api.evaluator_tools import CargoGitToolSpec


def _write_executable(path: Path, contents: str) -> None:
    path.write_text(contents, encoding="utf-8")
    path.chmod(0o755)


_DEBIAN_PACKAGE_NAMES = st.lists(
    st.sampled_from(["pkg-config", "libssl-dev", "libclang-dev", "cmake", "zlib1g-dev", "g++-12"]),
    unique=True,
    max_size=6,
)


def _run_container_setup_with_fake_apt(
    packages: list[str], *, already_installed: bool
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """Run a Docker-target setup under a real shell with Fake ``apt-get``/``dpkg``.

    The Fakes answer the package manager; python3, tar, cargo and rustc exist so
    only the system-package step has work to do. Returns the process and the
    ``apt-get`` argument lines it recorded.
    """
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        fake_bin = root / "fake-bin"
        fake_bin.mkdir()
        calls = root / "apt-calls"
        for name in ("python3", "tar"):
            _write_executable(fake_bin / name, "#!/bin/sh\nexit 0\n")
        _write_executable(fake_bin / "rustc", "#!/bin/sh\necho 'rustc 1.92.0 (fake)'\n")
        _write_executable(fake_bin / "cargo", "#!/bin/sh\necho 'cargo 1.92.0 (fake)'\n")
        _write_executable(fake_bin / "apt-get", f'#!/bin/sh\necho "$@" >> {calls}\n')
        _write_executable(fake_bin / "dpkg", f"#!/bin/sh\nexit {0 if already_installed else 1}\n")
        tool = CargoGitToolSpec(
            kind="cargo-git",
            git="https://example.com/tools/check.git",
            rev="a" * 40,
            package="check-tool",
            bins=("check",),
            system_packages=tuple(packages),
        )
        commands = evaluator_container_setup(
            TrustedEvaluatorRequirements(
                package_root=root / "evaluator-package",
                tools={"check-tool": tool},
                tools_root=root / "tools",
            )
        )
        # lint-waiver: LW-837290 [S603]; fixed local shell argv exercising the generated
        # setup contract, for the same reason as the rootless Rust setup test above.
        result = subprocess.run(  # noqa: S603
            ["/bin/sh", "-c", "set -e\n" + "\n".join(commands)],
            cwd=root,
            env={**os.environ, "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}"},
            capture_output=True,
            check=False,
            text=True,
            timeout=30,
        )
        recorded = calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []
    return result, recorded


@settings(max_examples=25, deadline=None)
@given(packages=_DEBIAN_PACKAGE_NAMES)
def test_docker_setup_installs_exactly_the_system_packages_a_tool_declares(
    packages: list[str],
) -> None:
    result, calls = _run_container_setup_with_fake_apt(packages, already_installed=False)

    assert result.returncode == 0, result.stderr
    installs = [call.split() for call in calls if "install" in call.split()]
    if not packages:
        assert installs == []
        return
    assert len(installs) == 1
    assert {argument for argument in installs[0] if not argument.startswith("-")} - {
        "install"
    } == set(packages)


@settings(max_examples=10, deadline=None)
@given(packages=_DEBIAN_PACKAGE_NAMES)
def test_docker_setup_does_not_touch_the_package_manager_when_packages_are_present(
    packages: list[str],
) -> None:
    result, calls = _run_container_setup_with_fake_apt(packages, already_installed=True)

    assert result.returncode == 0, result.stderr
    assert calls == []
