"""The observation boundary is bounded, and one damaged run stays that run's problem."""

import json
from dataclasses import dataclass
from pathlib import Path
from shutil import copytree
from tempfile import TemporaryDirectory

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from vs_proto.api import RUN_LOG_MAGIC, RunHead
from vs_runlog.api import FileRunLogWriter, HeadObserver, RunLogDamagedError, read_run_head
from vs_sim.api.testing import ManualClock, SimThreads

CORPUS = Path(__file__).resolve().parents[3] / "proto/testdata/retained"
CASES = json.loads((CORPUS / "manifest.json").read_text())
FENCES = st.integers(1, 18)


def reads_over_few_fences(
    *flags: st.SearchStrategy[bool],
) -> st.SearchStrategy[list[tuple[int, ...]]]:
    """Read sequences over at most four committed fences, so a fence repeats and interleaves.

    A scan costs the log's size, and the bound under test (never more scans than distinct
    prefixes) does not depend on how many prefixes there are, only on how reads revisit them.
    """
    return st.lists(FENCES, min_size=1, max_size=4, unique=True).flatmap(
        lambda fences: st.lists(st.tuples(st.sampled_from(fences), *flags), min_size=1, max_size=60)
    )


def stored_head(case: str) -> RunHead:
    head = RunHead.from_binary((CORPUS / case / "run.head").read_bytes())
    assert head.objective is not None
    head.objective.clear_field("input_value")
    head.objective.input_stated = False
    head.performance_stated = True
    return head


@pytest.fixture(scope="module", params=CASES)
def healthy(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> tuple[Path, RunHead]:
    """An undamaged copy of a retained run, shared by the examples that damage copies of it."""
    run = tmp_path_factory.mktemp("healthy") / "run"
    copytree(CORPUS / request.param, run, symlinks=True, ignore=lambda *_: {"skeleton.response.pb"})
    return run, read_run_head(run / "run.log")


@dataclass(frozen=True)
class Logged:
    """A retained run's log in a directory, and the epochs a fresh observer read from it so far."""

    directory: Path
    case: str
    fresh: dict[tuple[int, bool], object]


@pytest.fixture(scope="module", params=CASES)
def logged(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> Logged:
    """A directory holding a retained run's log, for examples that set its head before each read."""
    directory = tmp_path_factory.mktemp("logged")
    (directory / "run.log").write_bytes((CORPUS / request.param / "run.log").read_bytes())
    return Logged(directory, request.param, {})


def read_or_damage(log: Path) -> RunHead | RunLogDamagedError:
    try:
        return read_run_head(log)
    except RunLogDamagedError as error:
        return error


def damage(directory: Path, kind: str, at: int, noise: bytes) -> None:
    log, head = directory / "run.log", directory / "run.head"
    match kind:
        case "cut log":
            log.write_bytes(log.read_bytes()[:at])
        case "log garbage":
            data = bytearray(log.read_bytes())
            data[at : at + len(noise)] = noise
            log.write_bytes(bytes(data))
        case "cut head":
            head.write_bytes(head.read_bytes()[:at])
        case "head garbage":
            head.write_bytes(noise)
        case "fence past the log":
            raw = RunHead.from_binary(head.read_bytes())
            raw.committed_seq = at
            head.write_bytes(raw.to_binary())


@pytest.mark.regression
@example(kind="cut log", at=40_000, noise=b"")
@example(kind="fence past the log", at=19, noise=b"")
@example(kind="head garbage", at=0, noise=b"\xff\xff")
@given(
    kind=st.sampled_from(
        ["cut log", "log garbage", "cut head", "head garbage", "fence past the log"]
    ),
    at=st.integers(0, 90_000),
    noise=st.binary(max_size=64),
)
def test_a_damaged_run_raises_its_own_typed_error_and_leaves_others_readable(
    healthy: tuple[Path, RunHead], kind: str, at: int, noise: bytes
) -> None:
    """Truncation, garbage and a fence past the log: the error is typed and names the run."""
    run, before = healthy
    with TemporaryDirectory() as temporary:
        damaged = Path(temporary) / "damaged"
        copytree(run, damaged, symlinks=True)
        damage(damaged, kind, at, noise)
        outcome = read_or_damage(damaged / "run.log")
        if isinstance(outcome, RunLogDamagedError):
            assert str(damaged) in str(outcome)
        assert read_run_head(run / "run.log") == before


@pytest.mark.regression
@example(reads=[(14, False, True), (14, False, True), (18, True, True), (13, False, False)])
@given(reads=reads_over_few_fences(st.booleans(), st.booleans()))
def test_only_previous_writer_heads_scan_the_log_once_per_prefix(
    logged: Logged, reads: list[tuple[int, bool, bool]]
) -> None:
    """A head states its facts or it does not. Scans never exceed the distinct unstated prefixes."""
    directory, case = logged.directory, logged.case
    want = CASES[case]
    observer = HeadObserver()
    scanned: set[int] = set()
    for fence, stated, has_epoch in reads:
        head = stored_head(case)
        head.committed_seq = fence
        assert head.objective is not None
        head.objective.input_stated = stated
        if not has_epoch:
            head.epoch = 0
        (directory / "run.head").write_bytes(head.to_binary())
        got = observer.read(directory / "run.log")
        if not stated or not has_epoch:
            scanned.add(fence)
        assert got.epoch == 1
        assert got.objective is not None
        measured = got.objective.has_field("input_value")
        assert measured == (not stated and fence >= want["measurement_seq"])
        if measured:
            assert got.objective.input_value == want["input"]
    assert observer.scans <= len(scanned)


@pytest.mark.regression
@example(reads=[(14, True), (14, True), (18, False), (14, True), (18, False)])
@given(reads=reads_over_few_fences(st.booleans()))
def test_an_epoch_is_folded_once_per_committed_prefix(
    logged: Logged, reads: list[tuple[int, bool]]
) -> None:
    """Relaunch and the terminate poll read an epoch repeatedly; scans never exceed the prefixes."""
    directory, case, fresh = logged.directory, logged.case, logged.fresh
    observer = HeadObserver()
    for fence, headed in reads:
        if headed:
            head = stored_head(case)
            head.committed_seq = fence
            (directory / "run.head").write_bytes(head.to_binary())
        else:
            (directory / "run.head").unlink(missing_ok=True)
        # A fresh observer's epoch depends only on the fence and whether a head stands, so it is
        # read once per kind of state, however many examples repeat it.
        key = (fence, headed)
        if key not in fresh:
            fresh[key] = HeadObserver().current_epoch(directory / "run.log")
        assert observer.current_epoch(directory / "run.log") == fresh[key]
    assert observer.scans <= len({(fence if headed else None) for fence, headed in reads})


@pytest.mark.regression
@pytest.mark.parametrize("case", CASES)
def test_a_current_writer_head_never_opens_the_log(tmp_path: Path, case: str) -> None:
    """A log no scan could finish cannot matter to a head that states its facts."""
    head = stored_head(case)
    assert head.objective is not None
    head.objective.input_stated = True
    (tmp_path / "run.head").write_bytes(head.to_binary())
    (tmp_path / "run.log").write_bytes(RUN_LOG_MAGIC + b"\x00" * (4 << 20))
    observer = HeadObserver()
    for _ in range(3):
        assert observer.read(tmp_path / "run.log") == head
    assert observer.scans == 0


@pytest.mark.parametrize("case", CASES)
def test_a_previous_writer_head_scans_once_however_often_it_is_read(
    tmp_path: Path, case: str
) -> None:
    copytree(
        CORPUS / case, tmp_path / "run", symlinks=True, ignore=lambda *_: {"skeleton.response.pb"}
    )
    observer = HeadObserver()
    reads = [observer.read(tmp_path / "run" / "run.log") for _ in range(20)]
    assert all(read == reads[0] for read in reads)
    assert observer.scans == 1


@pytest.mark.parametrize("case", CASES)
def test_a_writer_resuming_a_previous_writers_run_publishes_the_facts_it_states(
    tmp_path: Path, case: str
) -> None:
    """The recovered log is upgraded as it folds, so the head carries the input without rescans."""
    copytree(
        CORPUS / case,
        tmp_path / "run",
        symlinks=True,
        ignore=lambda *_: {"skeleton.response.pb", "keyframes"},
    )
    path = tmp_path / "run" / "run.log"
    stored = stored_head(case).objective
    assert stored is not None
    assert not stored.input_stated
    with FileRunLogWriter(path, clock=ManualClock(), threads=SimThreads()):
        head = RunHead.from_binary((tmp_path / "run" / "run.head").read_bytes())
    assert head.objective is not None
    assert head.objective.input_stated
    assert head.objective.input_value == CASES[case]["input"]
