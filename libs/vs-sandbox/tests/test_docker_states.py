"""The Docker lifecycle translator: forward only, total, and never guessing."""

from __future__ import annotations

import tempfile
from pathlib import Path

from hypothesis import given
from hypothesis import strategies as st

from vs_sandbox.api import (
    INITIAL_CONTAINER_LIFE,
    ContainerGone,
    ContainerLife,
    ContainerRemoving,
    ContainerUnknown,
    DockerSandbox,
    RemovalReading,
    RemovedBy,
    observe_removal,
    released,
)
from vs_sandbox.api.testing import FakeDockerEngine
from vs_sim.api import DISCARD
from vs_sim.api.testing import FakePipedProcessLauncher, SimThreads

_MESSAGES = [
    "",
    "Error response from daemon: No such container: abc",
    "Error response from daemon: removal of container abc is already in progress",
    "Error response from daemon: removal of container abc failed",
    "Error: No such image: abc",
    "no such container",
    "daemon unavailable",
]
_READING = st.builds(
    RemovalReading,
    st.integers(-2, 130),
    st.one_of(st.sampled_from(_MESSAGES), st.text(max_size=30)),
)
_RANK = {"unknown": -1, "removing": 0, "gone": 1}


@given(readings=st.lists(_READING, max_size=30))
def test_observe_removal_is_monotone_and_gone_is_final(readings: list[RemovalReading]) -> None:
    held: ContainerLife = INITIAL_CONTAINER_LIFE
    for reading in readings:
        after = observe_removal(held, reading)
        assert _RANK[after.kind] >= _RANK[held.kind]
        if isinstance(held, ContainerGone):
            assert after == held
        if not isinstance(held, ContainerUnknown):
            assert not isinstance(after, ContainerUnknown)
        held = after


@given(reading=_READING)
def test_every_reading_maps_to_a_defined_life(reading: RemovalReading) -> None:
    life = observe_removal(INITIAL_CONTAINER_LIFE, reading)
    assert life.kind in set(_RANK)
    assert released(life) == (not isinstance(life, ContainerUnknown))


@given(returncode=st.integers(1, 130), detail=st.text(max_size=30))
def test_a_failure_with_text_the_daemon_never_prints_leaves_the_container_owned(
    returncode: int, detail: str
) -> None:
    """Text that names neither an absent nor a vanishing container proves nothing is gone."""
    stderr = f"Error response from daemon: {detail.replace('container', '')}"
    life = observe_removal(INITIAL_CONTAINER_LIFE, RemovalReading(returncode, stderr))
    assert not released(life)
    assert life == ContainerUnknown(raw=stderr.strip(), returncode=returncode)


@given(returncode=st.integers(1, 130), name=st.text("abcdef0123456789", min_size=1, max_size=12))
def test_the_daemons_own_messages_release_the_container(returncode: int, name: str) -> None:
    for stderr in (
        f"Error response from daemon: No such container: {name}",
        f"Error response from daemon: removal of container {name} is already in progress",
    ):
        life = observe_removal(INITIAL_CONTAINER_LIFE, RemovalReading(returncode, stderr))
        assert released(life)


def test_each_way_of_being_gone_has_its_own_outcome() -> None:
    assert observe_removal(INITIAL_CONTAINER_LIFE, RemovalReading(0)) == ContainerGone(
        outcome=RemovedBy.REMOVED
    )
    absent = RemovalReading(1, "Error response from daemon: No such container: abc")
    assert observe_removal(INITIAL_CONTAINER_LIFE, absent) == ContainerGone(
        outcome=RemovedBy.ALREADY_ABSENT
    )
    going = RemovalReading(1, "removal of container abc is already in progress")
    assert observe_removal(INITIAL_CONTAINER_LIFE, going) == ContainerRemoving()


@given(later=_READING)
def test_a_failed_retry_cannot_unremove_a_removed_container(later: RemovalReading) -> None:
    removed = observe_removal(INITIAL_CONTAINER_LIFE, RemovalReading(0))
    assert observe_removal(removed, later) == removed


def test_the_fake_engine_speaks_the_dialect_the_translator_reads() -> None:
    """The Fake's ``docker rm`` text translates like the real daemon's."""
    with tempfile.TemporaryDirectory() as raw:
        base = Path(raw)
        state = base / "engine"
        state.mkdir()
        workspace = base / "workspace"
        workspace.mkdir()
        engine = FakeDockerEngine(state, launcher=FakePipedProcessLauncher())
        sandbox = DockerSandbox(
            host_workspace=str(workspace), image="agent-image", docker=engine, threads=SimThreads()
        )
        sandbox.start(output=DISCARD)
        container = sandbox.container_id
        try:
            first = engine.run(
                ["docker", "rm", "-f", container], timeout_seconds=10, output=DISCARD
            )
            again = engine.run(
                ["docker", "rm", "-f", container], timeout_seconds=10, output=DISCARD
            )
        finally:
            sandbox.stop()
        removed = observe_removal(
            INITIAL_CONTAINER_LIFE, RemovalReading(first.returncode, first.stderr)
        )
        absent = observe_removal(
            INITIAL_CONTAINER_LIFE, RemovalReading(again.returncode, again.stderr)
        )

    assert removed == ContainerGone(outcome=RemovedBy.REMOVED)
    assert absent == ContainerGone(outcome=RemovedBy.ALREADY_ABSENT)
