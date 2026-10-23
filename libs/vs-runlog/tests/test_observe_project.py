"""Previous publishers need no address sidecar or manual rewrite to read their project."""

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from vs_project.api import Project
from vs_proto.api import RUN_LOG_MAGIC, EpochStarted, Lifecycle, Oneof, Record, RunHead, Running
from vs_runlog.api import HeadObserver, RunLogReader, encode_frame


@pytest.mark.regression
@example(name="unvisited project", headed=True, stored=False, typed=False)
@example(name="unvisited project", headed=False, stored=False, typed=True)
@given(
    name=st.text(alphabet="abcdefghijklmnopqrstuvwxyz 0123456789", min_size=1, max_size=30),
    headed=st.booleans(),
    stored=st.booleans(),
    typed=st.booleans(),
)
def test_project_survives_previous_publication(
    *, name: str, headed: bool, stored: bool, typed: bool
) -> None:
    """Generate canonical Project layouts, head generations and address presence."""
    with TemporaryDirectory() as temporary, pytest.MonkeyPatch.context() as environment:
        root = Path(temporary)
        environment.setenv("VIBESYS_STATE_HOME", str(root / "home"))
        project = root / f"project-{name}"
        project.mkdir()
        directory = Project.publish_run_for(project, "run-a").resolve()
        address = root / "home/run-addresses/run-a.pb"
        if not stored:
            address.unlink()
        log = directory / "run.log"
        record = Record(
            seq=1,
            epoch=1,
            body=Oneof("epoch_started", EpochStarted(epoch=1, project_root=str(project.resolve()))),
        )
        if typed:
            corpus = (
                Path(__file__).resolve().parents[3] / "proto/testdata/retained/438bfd3d/run.log"
            )
            with corpus.open("rb") as stream:
                record = next(iter(RunLogReader(stream)))
            assert record.body is not None
            assert record.body.field == "epoch_started"
            record.body.value.project_root = str(project.resolve())
        log.write_bytes(RUN_LOG_MAGIC + encode_frame(record))
        if headed:
            (directory / "run.head").write_bytes(
                RunHead(
                    strategy="search",
                    epoch=1,
                    committed_seq=1,
                    lifecycle=Lifecycle(state=Oneof("running", Running())),
                ).to_binary()
            )
        original = log.read_bytes()
        reader = HeadObserver()
        for _ in range(4):
            assert reader.project_root(log) == str(Project.open(project).root)
            if headed:
                assert reader.read(log).epoch == 1
        assert reader.scans == 1
        assert log.read_bytes() == original
        assert address.exists() == stored
