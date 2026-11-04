"""Checkout provenance is supplied at the host launch boundary."""

from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vibesys.api.request import checkout_identity
from vs_project.api import Git
from vs_sim.api import ProcessSpec
from vs_sim.api.testing import FakeProcesses, ManualClock, ProcessScript


@given(dirty=st.booleans())
def test_checkout_identity_names_the_tree_and_tracked_or_untracked_edits(*, dirty: bool) -> None:
    def script(spec: ProcessSpec) -> ProcessScript:
        return ProcessScript(
            stdout=(b"tree-a\n" if "rev-parse" in spec.argv else (b"?? new.py\n" if dirty else b""))
        )

    clock = ManualClock()
    processes = FakeProcesses(script, pass_time=clock.advance)
    assert checkout_identity(Path("/checkout"), Git(processes)) == ("tree-a", dirty)


@pytest.mark.parametrize("failure", ["rev-parse", "status"])
def test_checkout_identity_refuses_failed_provenance_probes(failure: str) -> None:
    clock = ManualClock()
    processes = FakeProcesses(
        lambda spec: ProcessScript(returncode=int(failure in spec.argv), stdout=b"tree-a\n"),
        pass_time=clock.advance,
    )
    with pytest.raises(RuntimeError, match="cannot identify the host checkout"):
        checkout_identity(Path("/checkout"), Git(processes))
