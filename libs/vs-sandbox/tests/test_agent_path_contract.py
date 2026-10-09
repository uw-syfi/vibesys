"""Host-local sandbox path mappings are identical in real and Fake execution."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_sandbox.api import LocalShellRunner
from vs_sandbox.api.testing import FakeCommandRunner

if TYPE_CHECKING:
    from collections.abc import Callable


def _local_shell() -> LocalShellRunner:
    return LocalShellRunner(Path.cwd())


@pytest.mark.parametrize("make", [FakeCommandRunner, _local_shell], ids=["fake", "local"])
@given(host_path=st.one_of(st.text(), st.text().map(Path)))
def test_host_agent_path_preserves_pathlib_path(
    make: Callable[[], FakeCommandRunner | LocalShellRunner], host_path: str | Path
) -> None:
    assert make().agent_path(host_path) == str(Path(host_path))
