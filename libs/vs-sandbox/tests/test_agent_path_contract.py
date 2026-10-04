"""Host-local sandbox path mappings are identical in real and Fake execution."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_sandbox.api import LocalShellSandbox
from vs_sandbox.api.testing import FakeSandbox

if TYPE_CHECKING:
    from collections.abc import Callable


def _local_shell() -> LocalShellSandbox:
    return LocalShellSandbox(Path.cwd())


@pytest.mark.parametrize("make", [FakeSandbox, _local_shell], ids=["fake", "local"])
@given(host_path=st.one_of(st.text(), st.text().map(Path)))
def test_host_agent_path_preserves_pathlib_path(
    make: Callable[[], FakeSandbox | LocalShellSandbox], host_path: str | Path
) -> None:
    assert make().agent_path(host_path) == str(Path(host_path))
