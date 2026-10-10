"""The core evaluation tool server starts wherever its descriptor says to start it."""

from __future__ import annotations

import os
import subprocess
import sys

from hypothesis import given
from hypothesis import strategies as st

from vs_evaluation.api.tools import CORE_MCP_PACKAGES, core_evaluation_mcp_descriptor

_PRINT_FRAMEWORK_IMPORTS = (
    "import importlib, sys; importlib.import_module(sys.argv[1]); "
    "print(*sorted({m.split('.')[0] for m in sys.modules if m.startswith('vs_')}))"
)

_roots = st.lists(
    st.from_regex(r"/[a-z]{1,8}(/[a-z]{1,8}){0,3}", fullmatch=True), min_size=1, max_size=4
)


def test_the_declared_packages_are_exactly_what_the_server_imports() -> None:
    descriptor = core_evaluation_mcp_descriptor("token", "/run/evaluation.sock")
    module = descriptor.args[descriptor.args.index("-m") + 1]

    # A fresh interpreter is the only way to see exactly what importing the module pulls in.
    imported = subprocess.run(  # noqa: S603  # LW-158203; the argv is this interpreter and a fixed program.
        # > Importing in-process would see this test session's modules, not the server's own
        # > closure; ModuleFinder also counts type-checking-only imports. A wrapper would only
        # > hide the single direct call, and shell=True would weaken the argv guarantee.
        [sys.executable, "-c", _PRINT_FRAMEWORK_IMPORTS, module],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()

    assert sorted(imported) == sorted(name.replace("-", "_") for name in CORE_MCP_PACKAGES)


@given(roots=_roots)
def test_import_roots_become_the_servers_python_path(roots: list[str]) -> None:
    descriptor = core_evaluation_mcp_descriptor("t", "/s", import_roots=roots)

    assert dict(descriptor.env) == {"PYTHONPATH": os.pathsep.join(roots)}
    assert dict(descriptor.runtime_env) == {
        "VS_EVALUATION_SOCKET": "/s",
        "VS_EVALUATION_TOKEN": "t",
    }


def test_a_server_without_import_roots_changes_no_python_path() -> None:
    assert core_evaluation_mcp_descriptor("t", "/s").env == ()
