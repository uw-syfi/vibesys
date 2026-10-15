"""One EvaluationLedger contract, run against the Fake and the ledger on the cluster's filesystem."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from vs_sim.api.testing import examples
from vs_slurm.api import (
    EvaluationLedger,
    FakeEvaluationLedger,
    LedgerError,
    LedgerLabels,
    LedgerLease,
    LedgerRecord,
    LedgerStage,
    LedgerState,
    RemoteEvaluationLedger,
    SlurmConfig,
    SlurmConnectorTransport,
    SubmitBoundJob,
    UnreadableLease,
    encode_ledger_document,
    ledger_operation_id,
)

# test-isolation: the public executable transport Fake gives the remote ledger a real filesystem.
from vs_slurm.fake_connector import FakeConnector

if TYPE_CHECKING:
    from collections.abc import Callable

PROJECT = "demo-0123456789ab"
KEY = "a" * 64
OTHER_KEY = "b" * 64
GOLDEN = Path(__file__).parent / "goldens" / "ledger_record_v1.json"


@dataclass
class _Case:
    ledger: EvaluationLedger
    reopen: Callable[[], EvaluationLedger]
    plant: Callable[[str, int, str], None]
    put_file: Callable[[str, bytes], str]
    # Plant a lease's bytes, its file last written at the given epoch second.
    plant_lease: Callable[[str, str, str, float], None]


def _make(implementation: str, root: Path) -> _Case:
    if implementation == "fake":
        fake = FakeEvaluationLedger(project=PROJECT)

        def put_fake(name: str, content: bytes) -> str:
            path = f"/site/{name}"
            fake.put_operator_file(path, content)
            return path

        def plant_fake_lease(key: str, holder: str, document: str, written_at: float) -> None:
            fake.store_raw_lease(key, holder, document, written_at=written_at)

        return _Case(fake, fake.reopen, fake.store_raw, put_fake, plant_fake_lease)
    remote = root / "remote"
    remote.mkdir()
    config = SlurmConfig(
        name="contract",
        remote_workspace_root=str(remote),
        transport=SlurmConnectorTransport(kind="connector", command=("fake-connector",)),
    )
    connector = FakeConnector(root / "connector")
    scratch = root / "scratch"
    scratch.mkdir()

    def open_ledger() -> EvaluationLedger:
        return RemoteEvaluationLedger(
            config, project=PROJECT, processes=connector, scratch_root=scratch
        )

    def plant(key: str, attempt: int, document: str) -> None:
        directory = remote / "contract" / "ledger" / PROJECT / key
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"attempt-{attempt}.0.json").write_text(document, encoding="utf-8")

    def put_file(name: str, content: bytes) -> str:
        path = root / "site" / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(content)
        return path.as_posix()

    def plant_lease(key: str, holder: str, document: str, written_at: float) -> None:
        directory = remote / "contract" / "ledger" / PROJECT / key / "leases"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{holder}.json"
        path.write_text(document, encoding="utf-8")
        os.utime(path, (written_at, written_at))

    return _Case(open_ledger(), open_ledger, plant, put_file, plant_lease)


IMPLEMENTATIONS = ("fake", "remote")


@pytest.fixture(params=IMPLEMENTATIONS)
def case(request: pytest.FixtureRequest, tmp_path: Path) -> _Case:
    return _make(str(request.param), tmp_path)


def _record(
    key: str = KEY,
    *,
    attempt: int = 0,
    owner: str = "run-a",
    state: LedgerState = LedgerState.SUBMITTING,
    with_job: bool | None = None,
) -> LedgerRecord:
    if with_job is None:
        with_job = state in {LedgerState.SUBMITTED, LedgerState.COMPLETED}
    return LedgerRecord(
        key=key,
        project=PROJECT,
        attempt=attempt,
        operation_id=ledger_operation_id(key, attempt),
        owner=owner,
        created_at=100.0,
        claim_expires_at=1000.0,
        labels=LedgerLabels(candidate="abc123", workstream="ws-1"),
        state=state,
        job=_JOB if with_job else None,
        reason="ended" if state in {LedgerState.FAILED, LedgerState.WITHDRAWN} else None,
    )


_JOB = SubmitBoundJob(
    job_id="7",
    config_identity="0" * 64,
    remote_workspace="/r/op/workspace",
    remote_status_path="/r/op/exit-code.txt",
    remote_log_path="/r/op/job.log",
    stop_on_failure=True,
    stages=(LedgerStage(name="s"),),
)


def test_an_unknown_request_reads_as_absent(case: _Case) -> None:
    assert case.ledger.read(KEY) is None
    assert case.ledger.list_keys() == ()


def test_a_claimed_record_is_read_back_by_another_process(case: _Case) -> None:
    record = _record()
    assert case.ledger.claim(record) == record
    entry = case.reopen().read(KEY)
    assert entry is not None
    assert entry.record == record
    assert entry.leases == ()
    assert case.reopen().list_keys() == (KEY,)


@given(owners=st.lists(st.sampled_from(("run-a", "run-b", "run-c")), min_size=2, max_size=5))
@settings(max_examples=examples(pr=10, nightly=100), deadline=None)
def test_of_many_claims_of_one_attempt_exactly_the_first_is_stored(owners: list[str]) -> None:
    """Two (or more) processes asking for one key get one attempt, and so one job."""
    for implementation in IMPLEMENTATIONS:
        with TemporaryDirectory() as directory:
            case = _make(implementation, Path(directory))
            ledgers = [case.reopen() for _ in owners]
            stored = [
                ledger.claim(_record(owner=owner))
                for ledger, owner in zip(ledgers, owners, strict=True)
            ]
            assert {record.owner for record in stored} == {owners[0]}
            entry = case.ledger.read(KEY)
            assert entry is not None
            assert entry.record.owner == owners[0]


def test_the_highest_attempt_is_the_current_one_and_earlier_ones_are_listed(case: _Case) -> None:
    case.ledger.claim(_record(state=LedgerState.SUBMITTING))
    claimed = case.ledger.read(KEY)
    assert claimed is not None
    assert case.ledger.replace(claimed, _record(state=LedgerState.WITHDRAWN))
    case.ledger.claim(_record(attempt=1, owner="run-b"))
    entry = case.reopen().read(KEY)
    assert entry is not None
    assert (entry.record.attempt, entry.record.owner) == (1, "run-b")
    assert entry.record.state is LedgerState.SUBMITTING
    assert entry.revision == 0
    assert entry.earlier == (_record(state=LedgerState.WITHDRAWN),)


_STATES = st.sampled_from(LedgerState)


@given(
    writers=st.lists(st.sampled_from((LedgerState.SUBMITTED, LedgerState.WITHDRAWN)), min_size=2)
)
@settings(max_examples=examples(pr=10, nightly=100), deadline=None)
def test_of_many_swaps_from_one_revision_exactly_the_first_is_stored(
    writers: list[LedgerState],
) -> None:
    """A submitter recording its job and a remover withdrawing it never overwrite each other."""
    for implementation in IMPLEMENTATIONS:
        with TemporaryDirectory() as directory:
            case = _make(implementation, Path(directory))
            case.ledger.claim(_record())
            read = [case.reopen().read(KEY) for _ in writers]
            won = [
                case.reopen().replace(entry, _record(state=state))
                for entry, state in zip(read, writers, strict=True)
                if entry is not None
            ]
            assert won == [True] + [False] * (len(writers) - 1)
            entry = case.reopen().read(KEY)
            assert entry is not None
            assert (entry.record.state, entry.revision) == (writers[0], 1)


@given(before=_STATES, after=_STATES)
@settings(max_examples=examples(pr=25, nightly=100), deadline=None)
def test_a_store_writes_only_the_transitions_the_state_machine_allows(
    before: LedgerState, after: LedgerState
) -> None:
    """Withdrawn and failed are final; nothing goes back to submitting."""
    allowed = {
        LedgerState.SUBMITTING: {
            LedgerState.SUBMITTED,
            LedgerState.FAILED,
            LedgerState.WITHDRAWN,
        },
        LedgerState.SUBMITTED: {
            LedgerState.COMPLETED,
            LedgerState.FAILED,
            LedgerState.WITHDRAWN,
        },
        LedgerState.COMPLETED: {LedgerState.WITHDRAWN},
        LedgerState.FAILED: set(),
        LedgerState.WITHDRAWN: set(),
    }
    for implementation in IMPLEMENTATIONS:
        with TemporaryDirectory() as directory:
            case = _make(implementation, Path(directory))
            case.plant(KEY, 0, encode_ledger_document(_record(state=before)))
            entry = case.reopen().read(KEY)
            assert entry is not None
            named = {LedgerState.SUBMITTED, LedgerState.COMPLETED}
            moved = _record(
                state=after,
                with_job=after in named
                or (before in named and after is not LedgerState.SUBMITTING),
            )
            if after in allowed[before]:
                assert case.ledger.replace(entry, moved)
            else:
                with pytest.raises(LedgerError):
                    case.ledger.replace(entry, moved)
                current = case.reopen().read(KEY)
                assert current is not None
                assert current.record.state is before


def test_a_lease_that_cannot_be_read_is_absent_not_malformed(tmp_path: Path) -> None:
    """The remote read reads each lease in one step, so one that is gone by then is skipped.

    A lease that exists but cannot be read stands for one a release removed
    between the listing and the read: the old two-step read printed it with an
    empty document, and the whole read failed as malformed.
    """
    if os.geteuid() == 0:
        pytest.skip("root reads a file without read permission")
    case = _make("remote", tmp_path)
    case.ledger.claim(_record())
    case.ledger.renew(KEY, LedgerLease(holder="run-a", renewed_at=1.0, expires_at=301.0))
    leases = tmp_path / "remote" / "contract" / "ledger" / PROJECT / KEY / "leases"
    gone = leases / "run-gone.json"
    gone.write_text("{}", encoding="utf-8")
    gone.chmod(0)
    entry = case.reopen().read(KEY)
    assert entry is not None
    assert [lease.holder for lease in entry.leases] == ["run-a"]


@pytest.mark.parametrize(
    "document",
    [
        '{"format_version": 2, "holder": "run-new", "lease_kind": "slot"}\n',
        '{"holder": "run-new"}\n',
        "not json\n",
    ],
    ids=["newer_format", "no_version", "damaged"],
)
def test_a_lease_this_build_cannot_decode_is_a_holder_of_unknown_kind(
    case: _Case, document: str
) -> None:
    """One undecodable lease never fails the read: it is reported, and the others stand."""
    case.ledger.claim(_record())
    case.ledger.renew(KEY, LedgerLease(holder="run-a", renewed_at=1.0, expires_at=301.0))
    case.plant_lease(KEY, "run-new", document, 1_700_000_000.0)
    entry = case.reopen().read(KEY)
    assert entry is not None
    assert [lease.holder for lease in entry.leases] == ["run-a"]
    # When its file was last written bounds how long it holds the request.
    assert entry.unreadable_leases == (UnreadableLease("run-new", 1_700_000_000.0),)
    assert entry.record == _record()


def test_leases_are_per_holder_and_released_one_at_a_time(case: _Case) -> None:
    case.ledger.claim(_record())
    case.ledger.renew(KEY, LedgerLease(holder="run-a", renewed_at=1.0, expires_at=301.0))
    case.reopen().renew(KEY, LedgerLease(holder="run-b", renewed_at=2.0, expires_at=302.0))
    case.ledger.renew(KEY, LedgerLease(holder="run-a", renewed_at=5.0, expires_at=305.0))
    entry = case.ledger.read(KEY)
    assert entry is not None
    assert [(lease.holder, lease.expires_at) for lease in entry.leases] == [
        ("run-a", 305.0),
        ("run-b", 302.0),
    ]
    case.ledger.release(KEY, "run-a")
    entry = case.reopen().read(KEY)
    assert entry is not None
    assert [lease.holder for lease in entry.leases] == ["run-b"]


def test_requests_are_listed_per_project(case: _Case) -> None:
    case.ledger.claim(_record(OTHER_KEY))
    case.ledger.claim(_record(KEY))
    assert case.ledger.list_keys() == (KEY, OTHER_KEY)


def test_a_record_written_in_format_1_is_readable(case: _Case) -> None:
    """The pinned format-1 bytes stay readable by every later build."""
    golden = GOLDEN.read_text(encoding="utf-8")
    case.plant(KEY, 0, golden)
    entry = case.reopen().read(KEY)
    assert entry is not None
    assert entry.record.format_version == 1
    assert entry.record.state is LedgerState.SUBMITTED
    assert entry.record.job is not None
    assert entry.record.job.job_id == "4242"
    assert entry.record.labels == LedgerLabels(candidate="0f3c9e1", workstream="baseline")


@pytest.mark.parametrize(
    "document",
    [
        '{"format_version": 2, "key": "' + KEY + '"}\n',
        '{"key": "' + KEY + '"}\n',
        "not json\n",
        '{"format_version": 1, "key": "' + KEY + '", "unexpected": true}\n',
    ],
)
def test_a_record_this_build_cannot_read_is_an_error_not_an_absence(
    case: _Case, document: str
) -> None:
    case.plant(KEY, 0, document)
    with pytest.raises(LedgerError):
        case.reopen().read(KEY)


@pytest.mark.parametrize("name", ["", "../escape", "a" * 63, "A" * 64])
def test_a_key_that_is_not_a_digest_is_refused(case: _Case, name: str) -> None:
    with pytest.raises(LedgerError):
        case.ledger.read(name)


@given(content=st.binary(max_size=64))
@settings(max_examples=examples(pr=5, nightly=50), deadline=None)
def test_an_operator_file_digest_is_its_content_hash_and_absence_is_none(content: bytes) -> None:
    for implementation in IMPLEMENTATIONS:
        with TemporaryDirectory() as directory:
            case = _make(implementation, Path(directory))
            path = case.put_file("setup.sh", content)
            assert case.reopen().operator_file_sha256(path) == hashlib.sha256(content).hexdigest()
            missing = path.replace("setup.sh", "missing.sh")
            assert case.reopen().operator_file_sha256(missing) is None
