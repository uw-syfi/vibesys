"""One EvaluationLedger contract, run against the Fake and the ledger on the cluster's filesystem."""

from __future__ import annotations

import hashlib
import math
import os
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from vs_sim.api.testing import ManualClock, examples
from vs_slurm.api import (
    MAX_WITHDRAWAL_WINDOW_SECONDS,
    AttachJob,
    AwaitWithdrawal,
    CheckJob,
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
    WithdrawalMark,
    WithdrawalTiming,
    WithdrawalUndone,
    encode_ledger_document,
    ledger_operation_id,
    submit_step,
    undo_expired_withdrawal,
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
    # Plant a withdrawal revision's bytes (key, attempt, revision, document), written at a second.
    plant_withdrawal: Callable[[str, int, int, str, float], None]
    # The remote ledger's connector, through which a test injects faults; None for the Fake.
    connector: FakeConnector | None = None


def _make(implementation: str, root: Path, clock: ManualClock | None = None) -> _Case:
    """The case of ``implementation``; ``clock`` is its readers' clock (a fixed one by default)."""
    clock = clock if clock is not None else ManualClock(_WRITTEN_AT)
    if implementation == "fake":
        fake = FakeEvaluationLedger(project=PROJECT, clock=clock)

        def put_fake(name: str, content: bytes) -> str:
            path = f"/site/{name}"
            fake.put_operator_file(path, content)
            return path

        def plant_fake_lease(key: str, holder: str, document: str, written_at: float) -> None:
            fake.store_raw_lease(key, holder, document, written_at=written_at)

        def plant_fake_withdrawal(
            key: str, attempt: int, revision: int, document: str, written_at: float
        ) -> None:
            fake.store_raw_withdrawal(key, attempt, revision, document, written_at=written_at)

        return _Case(
            fake, fake.reopen, fake.store_raw, put_fake, plant_fake_lease, plant_fake_withdrawal
        )
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
            config, project=PROJECT, processes=connector, clock=clock, scratch_root=scratch
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

    def plant_withdrawal(
        key: str, attempt: int, revision: int, document: str, written_at: float
    ) -> None:
        directory = remote / "contract" / "ledger" / PROJECT / key
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"withdrawal-{attempt}.{revision}.json"
        path.write_text(document, encoding="utf-8")
        os.utime(path, (written_at, written_at))

    return _Case(
        open_ledger(), open_ledger, plant, put_file, plant_lease, plant_withdrawal, connector
    )


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


def _mark(attempt: int = 0, *, by: str = "run-b") -> WithdrawalMark:
    return WithdrawalMark(
        attempt=attempt, by=by, writer="0f3c9e1", since=100.0, until=700.0, reason="gone"
    )


_LIVE = (LedgerState.SUBMITTING, LedgerState.SUBMITTED, LedgerState.COMPLETED)


@pytest.mark.parametrize("state", _LIVE)
def test_a_withdrawal_mark_and_its_undoing_are_read_back_beside_an_unchanged_record(
    case: _Case, state: LedgerState
) -> None:
    """A mark is a document of its own: the record, and its revision, stay as they were."""
    planted = _record(state=state)
    case.plant(KEY, 0, encode_ledger_document(planted))
    live = case.reopen().read(KEY)
    assert live is not None
    assert (live.withdrawal, live.withdrawal_revision) == (None, -1)

    assert case.reopen().write_withdrawal(live, _mark())
    marked = case.reopen().read(KEY)
    assert marked is not None
    assert (marked.record, marked.revision) == (planted, 0)
    assert (marked.withdrawal, marked.withdrawal_revision) == (_mark(), 0)

    assert case.reopen().write_withdrawal(marked, WithdrawalUndone(attempt=0))
    undone = case.reopen().read(KEY)
    assert undone is not None
    assert (undone.record, undone.revision) == (planted, 0)
    assert (undone.withdrawal, undone.withdrawal_revision) == (None, 1)


def test_a_mark_on_an_earlier_attempt_is_not_the_current_attempts(case: _Case) -> None:
    case.ledger.claim(_record())
    claimed = case.ledger.read(KEY)
    assert claimed is not None
    assert case.ledger.write_withdrawal(claimed, _mark())
    marked = case.ledger.read(KEY)
    assert marked is not None
    assert case.ledger.replace(marked, _record(state=LedgerState.WITHDRAWN))
    case.ledger.claim(_record(attempt=1, owner="run-b"))
    entry = case.reopen().read(KEY)
    assert entry is not None
    assert entry.record.attempt == 1
    assert (entry.withdrawal, entry.withdrawal_revision) == (None, -1)


_WRITTEN_AT = 1_700_000_000.0
# Withdrawal revisions this build cannot decode: a newer build's mark, one with no
# version, a damaged file, and a mark of this format filed under another attempt.
_UNREADABLE_WITHDRAWALS = (
    '{"format_version": 2, "kind": "mark", "attempt": 0, "by": "run-new"}\n',
    '{"kind": "mark", "attempt": 0}\n',
    "not json\n",
    encode_ledger_document(_mark(3)),
)


@pytest.mark.parametrize("document", _UNREADABLE_WITHDRAWALS)
def test_a_withdrawal_revision_this_build_cannot_read_is_a_withdrawal_under_way(
    case: _Case, document: str
) -> None:
    """It never fails the read: no run attaches until one claim window after it was written.

    Then anyone may undo it, as an expired mark, and the request is attached to
    again. The record stays as it was.
    """
    planted = _record(state=LedgerState.SUBMITTED)
    case.plant(KEY, 0, encode_ledger_document(planted))
    case.plant_withdrawal(KEY, 0, 0, document, _WRITTEN_AT)

    entry = case.reopen().read(KEY)

    assert entry is not None
    assert (entry.record, entry.revision, entry.withdrawal_revision) == (planted, 0, 0)
    held_until = _WRITTEN_AT + entry.claim_window
    assert submit_step(entry, now=held_until) == AwaitWithdrawal(entry, expired=False)
    assert submit_step(entry, now=held_until + 1) == AwaitWithdrawal(entry, expired=True)
    assert not undo_expired_withdrawal(case.reopen(), entry, now=held_until)
    assert undo_expired_withdrawal(case.reopen(), entry, now=held_until + 1)
    undone = case.reopen().read(KEY)
    assert undone is not None
    assert (undone.record, undone.withdrawal, undone.withdrawal_revision) == (planted, None, 1)
    assert isinstance(submit_step(undone, now=held_until + 1), CheckJob | AttachJob)


@pytest.mark.parametrize("document", _UNREADABLE_WITHDRAWALS)
def test_a_withdrawal_revision_this_build_cannot_read_stands_only_as_its_attempts_newest(
    case: _Case, document: str
) -> None:
    """Under a newer revision this build reads, or on an earlier attempt, it changes nothing."""
    case.plant(KEY, 0, encode_ledger_document(_record(state=LedgerState.WITHDRAWN)))
    case.plant_withdrawal(KEY, 0, 0, document, _WRITTEN_AT)
    case.plant(KEY, 1, encode_ledger_document(_record(attempt=1, state=LedgerState.SUBMITTED)))
    case.plant_withdrawal(KEY, 1, 0, document, _WRITTEN_AT)
    case.plant_withdrawal(
        KEY, 1, 1, encode_ledger_document(WithdrawalUndone(attempt=1)), _WRITTEN_AT
    )

    entry = case.reopen().read(KEY)

    assert entry is not None
    assert (entry.record.attempt, entry.withdrawal, entry.withdrawal_revision) == (1, None, 1)


@given(withdrawers=st.lists(st.sampled_from(("run-b", "run-c", "vibesys-sweep")), min_size=2))
@settings(max_examples=examples(pr=10, nightly=100), deadline=None)
def test_of_many_marks_from_one_revision_exactly_the_first_is_stored(
    withdrawers: list[str],
) -> None:
    """Two withdrawers deciding on one read never both believe they hold the mark."""
    for implementation in IMPLEMENTATIONS:
        with TemporaryDirectory() as directory:
            case = _make(implementation, Path(directory))
            case.ledger.claim(_record())
            read = [case.reopen().read(KEY) for _ in withdrawers]
            won = [
                case.reopen().write_withdrawal(entry, _mark(by=by))
                for entry, by in zip(read, withdrawers, strict=True)
                if entry is not None
            ]
            assert won == [True] + [False] * (len(withdrawers) - 1)
            entry = case.reopen().read(KEY)
            assert entry is not None
            assert entry.withdrawal == _mark(by=withdrawers[0])


@given(
    state=_STATES,
    marked=st.booleans(),
    kind=st.sampled_from(("mark", "undone")),
    attempt=st.sampled_from((0, 1)),
)
@settings(max_examples=examples(pr=25, nightly=100), deadline=None)
def test_a_store_writes_only_the_withdrawal_revisions_that_follow_the_entry(
    state: LedgerState, *, marked: bool, kind: str, attempt: int
) -> None:
    """A mark goes on the live current attempt with none standing; an undoing follows a mark."""
    for implementation in IMPLEMENTATIONS:
        with TemporaryDirectory() as directory:
            case = _make(implementation, Path(directory))
            case.plant(KEY, 0, encode_ledger_document(_record(state=state)))
            entry = case.reopen().read(KEY)
            assert entry is not None
            if marked and not state.final:
                assert case.ledger.write_withdrawal(entry, _mark())
                entry = case.reopen().read(KEY)
                assert entry is not None
            standing = entry.withdrawal is not None
            document = _mark(attempt) if kind == "mark" else WithdrawalUndone(attempt=attempt)
            allowed = attempt == 0 and (
                (kind == "mark" and not state.final and not standing)
                or (kind == "undone" and standing)
            )
            if allowed:
                assert case.ledger.write_withdrawal(entry, document)
            else:
                with pytest.raises(LedgerError):
                    case.ledger.write_withdrawal(entry, document)


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


# r3206 LOW-A: documents a newline splits. The remote read printed one line per file, so
# each failed the whole read as a malformed reply.
_MULTI_LINE_WITHDRAWALS = (
    '{\n  "format_version": 2,\n  "kind": "mark",\n  "attempt": 0,\n  "by": "run-new"\n}\n',
    '{"format_version": 2,\n"kind": "mark"}',
    "damaged\n\n\x01 attempt-0.0.json \x7f\n",
)


@pytest.mark.parametrize("document", _MULTI_LINE_WITHDRAWALS)
def test_a_multi_line_withdrawal_revision_or_lease_never_fails_the_read(
    case: _Case, document: str
) -> None:
    planted = _record(state=LedgerState.SUBMITTED)
    case.plant(KEY, 0, encode_ledger_document(planted))
    case.plant_withdrawal(KEY, 0, 0, document, _WRITTEN_AT)
    case.plant_lease(KEY, "run-new", document, _WRITTEN_AT)

    entry = case.reopen().read(KEY)

    assert entry is not None
    assert (entry.record, entry.withdrawal_revision) == (planted, 0)
    assert isinstance(submit_step(entry, now=_WRITTEN_AT), AwaitWithdrawal)
    assert entry.unreadable_leases == (UnreadableLease("run-new", _WRITTEN_AT),)


def _record_with_claim_window(seconds: float) -> LedgerRecord:
    """A submitted record whose claim window, its claiming run's ``claim_seconds``, is ``seconds``."""
    record = _record(state=LedgerState.SUBMITTED)
    return record.model_copy(update={"claim_expires_at": record.created_at + seconds})


@pytest.mark.parametrize("claim_seconds", [60.0, 300.0, 829.0])
def test_an_unreadable_withdrawal_holds_for_at_least_any_withdrawers_window(
    case: _Case, claim_seconds: float
) -> None:
    """Its writer may be a build whose mark holds a whole window: it is not undone before that.

    The record's claim window is the claiming run's own setting, and may be
    shorter than any withdrawer's mark window.
    """
    window = WithdrawalTiming(clock=ManualClock(0.0), call_seconds=110.0).window_seconds
    case.plant(KEY, 0, encode_ledger_document(_record_with_claim_window(claim_seconds)))
    case.plant_withdrawal(KEY, 0, 0, _UNREADABLE_WITHDRAWALS[0], _WRITTEN_AT)

    entry = case.reopen().read(KEY)

    assert entry is not None
    assert submit_step(entry, now=_WRITTEN_AT + window) == AwaitWithdrawal(entry, expired=False)
    assert not undo_expired_withdrawal(case.reopen(), entry, now=_WRITTEN_AT + window)


def _fail_on_the_cluster(connector: FakeConnector, *programs: str) -> None:
    """Make each of ``programs`` fail in the ledger's reads on the cluster, as on a host without it.

    A shell function of the same name shadows the program in the read's remote command.
    """
    shadow = "".join(f"{program}() {{ return 1; }}; " for program in programs)

    def intercept(
        request: dict[str, object], answer: Callable[[dict[str, object]], dict[str, object]]
    ) -> dict[str, object]:
        command = str(request.get("command") or "")
        if request.get("operation") == "exec" and "withdrawal-*.json" in command:
            request = {**request, "command": shadow + command}
        return answer(request)

    connector.intercept(intercept)


def test_an_unreadable_withdrawal_is_held_by_its_file_time_on_a_host_whose_date_cannot_read_it(
    tmp_path: Path,
) -> None:
    """A login host whose ``date`` has no ``-r`` (BSD, a minimal image) still reads ``stat``."""
    case = _make("remote", tmp_path)
    assert case.connector is not None
    case.plant(KEY, 0, encode_ledger_document(_record(state=LedgerState.SUBMITTED)))
    case.plant_withdrawal(KEY, 0, 0, _UNREADABLE_WITHDRAWALS[0], _WRITTEN_AT)
    _fail_on_the_cluster(case.connector, "date")

    entry = case.reopen().read(KEY)

    assert entry is not None
    held_until = _WRITTEN_AT + max(entry.claim_window, MAX_WITHDRAWAL_WINDOW_SECONDS)
    assert submit_step(entry, now=held_until) == AwaitWithdrawal(entry, expired=False)
    assert submit_step(entry, now=held_until + 1) == AwaitWithdrawal(entry, expired=True)


@pytest.mark.parametrize("implementation", IMPLEMENTATIONS)
@given(
    document=st.sampled_from(_UNREADABLE_WITHDRAWALS),
    later=st.floats(min_value=0.0, max_value=3 * MAX_WITHDRAWAL_WINDOW_SECONDS),
)
@settings(max_examples=examples(pr=5, nightly=50), deadline=None)
def test_an_unreadable_withdrawal_whose_file_time_is_unknown_holds_from_when_each_reader_first_saw_it(
    implementation: str, document: str, later: float
) -> None:
    """Neither ``stat`` nor ``date -r`` reads its time: it is held for the longest mark window.

    Each reader holds it from its own first read of it, on its own clock, which
    is after the file was written, so it is never undone while its writer may
    still cancel; and it is undone once that much time passed, so it never
    blocks the request for ever.
    """
    with TemporaryDirectory() as directory:
        clock = ManualClock(_WRITTEN_AT)
        case = _make(implementation, Path(directory), clock)
        planted = _record(state=LedgerState.SUBMITTED)
        case.plant(KEY, 0, encode_ledger_document(planted))
        if case.connector is None:
            case.plant_withdrawal(KEY, 0, 0, document, math.inf)
        else:
            case.plant_withdrawal(KEY, 0, 0, document, _WRITTEN_AT)
            _fail_on_the_cluster(case.connector, "stat", "date")
        first_seen = clock.now()
        reader = case.reopen()

        entry = reader.read(KEY)
        clock.advance(later)
        again = reader.read(KEY)
        newcomer = case.reopen().read(KEY)

        assert entry is not None
        assert again is not None
        assert newcomer is not None
        held_until = first_seen + max(entry.claim_window, MAX_WITHDRAWAL_WINDOW_SECONDS)
        for read in (entry, again):  # one reader holds it from its first read, however often
            assert submit_step(read, now=held_until) == AwaitWithdrawal(read, expired=False)
            assert submit_step(read, now=held_until + 1) == AwaitWithdrawal(read, expired=True)
        newcomer_until = held_until + later
        assert submit_step(newcomer, now=newcomer_until) == AwaitWithdrawal(newcomer, expired=False)
        assert undo_expired_withdrawal(reader, again, now=held_until + 1)
        undone = reader.read(KEY)
        assert undone is not None
        assert isinstance(submit_step(undone, now=held_until + 1), CheckJob | AttachJob)
