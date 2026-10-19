"""The journal's span index: ``runs.events_for_span`` and ``runs.read_tail`` over ``run-events.jsonl``.

The journal is the truth and the index a disposable read model of it, so every property here
compares an answer the index gives with one computed from the journal's own lines: a span's
events are the journal filtered by span, tail pages concatenate to the journal, and whatever
state a crash, a resume, a failed write or a replaced journal leaves the sidecar in, the
answer is the one a full scan gives.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from tests.server.host_helper_support import fake_host, project_with_run
from tests.server.support import build_server_parts
from tests.server.test_run_trace import run_to_end

from server.host_api import (
    HELPER_LINE_MAX_BYTES,
    HELPER_REQUEST_FEATURES,
    RUN_EVENTS_FILE,
    RUN_EVENTS_READ_MAX_BYTES,
    HelperError,
    HelperErrorCode,
    HelperFeature,
    HelperReply,
    HelperRequest,
    request_features,
)
from server.host_helper_os import FileProjects
from server.host_helper_requests import answer
from server.host_run_events import (
    RUN_EVENTS_READ_MAX,
    RunsEventsForSpanReply,
    RunsEventsForSpanRequest,
    RunsReadTailReply,
    RunsReadTailRequest,
)
from server.span_index import (
    SpanIndexLog,
    load_journal_index,
    read_documents,
    sidecar_path,
    span_page,
)
from vibesys.events import AgentOutputChunkData, CoreEventType, ToolCallData
from vs_project.api import Project
from vs_project.api.testing import FakeGitRepositories
from vs_sim.api.testing import SimNetwork, SimThreads, examples

if TYPE_CHECKING:
    from pathlib import Path
    from random import Random

    import pytest
    from pydantic import JsonValue

    from server.span_index import JournalIndex

type _Document = dict[str, JsonValue]

_RUN_ID = "20261010-120000-11111111-dynamic"
_SPANS = ("op:a", "op:b", "op:c", "flow:root#1", "flow:mid#1")
_EXECUTIONS = ("e1", "e2", "e3")
_SPAN_EVENT_TYPES = ("span_started", "span_updated", "span_ended")
_SLOW = settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    max_examples=examples(pr=40, nightly=300),
    deadline=None,
)


def _line(document: _Document) -> bytes:
    return json.dumps(document, separators=(",", ":")).encode()


def _span_event(sequence: int, span: str, parent: str | None, invocation: str | None) -> _Document:
    return {
        "sequence": sequence,
        "type": "span_started",
        "data": {
            "kind": "span_started",
            "span": {"id": span, "parent": parent, "invocation": invocation},
        },
    }


def _turn_event(sequence: int, execution: str | None, span: str | None, text: str) -> _Document:
    document: _Document = {"sequence": sequence, "type": "agent_output_chunk", "text": text}
    if execution is not None:
        document["execution_id"] = execution
    if span is not None:
        document["span_id"] = span
    return document


@st.composite
def _journals(draw: st.DrawFn, *, nonce: str = "") -> list[_Document]:
    """A journal's events: span events naming parents and invocations, and turn events."""
    documents: list[_Document] = []
    sequence = 0
    for _ in range(draw(st.integers(0, 40))):
        sequence += draw(st.integers(1, 3))
        if draw(st.integers(0, 9)) == 0:  # a resumed run's older store restarted its numbering
            sequence = draw(st.integers(0, 3))
        if draw(st.integers(0, 4)) == 0:
            documents.append(
                _span_event(
                    sequence,
                    draw(st.sampled_from(_SPANS)),
                    draw(st.none() | st.sampled_from(_SPANS)),
                    draw(st.none() | st.sampled_from(_EXECUTIONS)),
                )
            )
        else:
            text = draw(st.text(max_size=60)) + nonce
            documents.append(
                _turn_event(
                    sequence,
                    draw(st.none() | st.sampled_from(_EXECUTIONS)),
                    draw(st.none() | st.sampled_from(_SPANS)),
                    text,
                )
            )
    return documents


def _as_read(documents: list[_Document]) -> list[_Document]:
    """The journal as a reader sees it: an event that does not pass its predecessor's sequence
    is the next after it, as the event store repairs a journal an older store restarted."""
    seen: list[_Document] = []
    last = 0
    for document in documents:
        sequence = document["sequence"]
        assert isinstance(sequence, int)
        last = sequence if sequence > last else last + 1
        seen.append({**document, "sequence": last})
    return seen


def _owned_by(documents: list[_Document], span: str) -> list[_Document]:
    """The journal filtered by span, computed from the documents alone.

    An event is a span's when it names a span at or beneath it (parents as the first span
    event naming a span recorded them), or names none and ran as the invocation of one.
    """
    parent: dict[str, str | None] = {}
    invocation: dict[str, str] = {}
    for document in documents:
        recorded = _recorded_span(document)
        if recorded is not None:
            identity, above, ran_as = recorded
            parent.setdefault(identity, above)
            if ran_as is not None:
                invocation[identity] = ran_as
    under = {span}
    while True:
        grown = under | {child for child, above in parent.items() if above in under}
        if grown == under:
            break
        under = grown
    invocations = {invocation[member] for member in under if member in invocation}
    return [
        document
        for document in documents
        if document.get("type") not in _SPAN_EVENT_TYPES
        and (
            document.get("span_id") in under
            if "span_id" in document
            else document.get("execution_id") in invocations
        )
    ]


def _recorded_span(document: _Document) -> tuple[str, str | None, str | None] | None:
    """The span a span event records: its id, parent and invocation."""
    data = document.get("data")
    if not isinstance(data, dict):
        return None
    span = data.get("span")
    if not isinstance(span, dict):
        return None
    identity, above, ran_as = span["id"], span["parent"], span["invocation"]
    assert isinstance(identity, str)
    assert above is None or isinstance(above, str)
    assert ran_as is None or isinstance(ran_as, str)
    return identity, above, ran_as


@dataclass
class _Journal:
    """A journal file as its writer extends it: events appended, each recorded in the sidecar."""

    path: Path
    log: SpanIndexLog

    @classmethod
    def open(cls, path: Path) -> _Journal:
        return cls(path, SpanIndexLog(path))

    def append(self, document: _Document) -> None:
        line = _line(document)
        size = self.path.stat().st_size if self.path.exists() else 0
        with self.path.open("ab") as stream:
            stream.write(line + b"\n")
        self.log.append(size, line)

    def resume(self) -> _Journal:
        """Reopen after a crash: a torn last line is cut off, an unterminated whole one ended."""
        data = self.path.read_bytes() if self.path.exists() else b""
        if data and not data.endswith(b"\n"):
            kept = data.rfind(b"\n") + 1
            try:
                json.loads(data[kept:])
            except ValueError:
                self.path.write_bytes(data[:kept])
            else:
                self.path.write_bytes(data + b"\n")
        return _Journal.open(self.path)


def _write(path: Path, documents: list[_Document]) -> None:
    """The journal as a run's writer leaves it."""
    path.touch()
    journal = _Journal.open(path)
    for document in documents:
        journal.append(document)


def _scan(path: Path) -> JournalIndex:
    """The index a reader builds from the journal alone: a copy of it with no sidecar."""
    lone = path.parent / "lone"
    lone.mkdir(exist_ok=True)
    (lone / path.name).write_bytes(path.read_bytes())
    return load_journal_index(lone / path.name)


def _entries(index: JournalIndex) -> list[object]:
    return list(index.entries)


def _documents_of(path: Path) -> list[_Document]:
    return [json.loads(line) for line in path.read_bytes().splitlines() if line]


@_SLOW
@given(
    documents=_journals(),
    count=st.integers(1, 6),
    max_bytes=st.integers(1, 700),
)
def test_a_spans_pages_concatenate_to_the_journal_filtered_by_span(
    tmp_path_factory: pytest.TempPathFactory, documents: list[_Document], count: int, max_bytes: int
) -> None:
    path = tmp_path_factory.mktemp("journal") / RUN_EVENTS_FILE
    _write(path, documents)
    index = load_journal_index(path)
    documents = _as_read(documents)

    for span in _SPANS:
        expected = _owned_by(documents, span)
        read: list[_Document] = []
        from_sequence = 1
        for _page in range(len(documents) + 2):
            entries, total, complete = span_page(index, span, from_sequence, count, max_bytes)
            assert total == len(expected)
            assert len(entries) <= count
            assert len(entries) <= 1 or sum(entry.length + 1 for entry in entries) <= max_bytes
            assert bool(entries) or complete
            read.extend(read_documents(path, entries))
            if complete:
                break
            from_sequence = entries[-1].sequence + 1
        assert read == expected


@_SLOW
@given(
    documents=_journals(),
    count=st.integers(1, 6),
    max_bytes=st.integers(1, 700),
)
def test_tail_pages_concatenate_to_the_journal_without_gaps_or_duplicates(
    tmp_path_factory: pytest.TempPathFactory, documents: list[_Document], count: int, max_bytes: int
) -> None:
    path = tmp_path_factory.mktemp("journal") / RUN_EVENTS_FILE
    _write(path, documents)
    index = load_journal_index(path)

    pages: list[list[_Document]] = []
    before: int | None = None
    for _page in range(len(documents) + 2):
        entries, older = index.tail(before, count, max_bytes)
        assert len(entries) <= count
        assert len(entries) <= 1 or sum(entry.length + 1 for entry in entries) <= max_bytes
        assert bool(entries) or not older
        pages.append(read_documents(path, entries))
        if not older:
            break
        before = entries[0].sequence
    assert [document for page in reversed(pages) for document in page] == _as_read(documents)


_DAMAGES = (
    "none",
    "drop_sidecar",
    "cut_sidecar",
    "garbage_sidecar",
    "damage_source_stamp",
    "tear_journal",
    "other_run",
)


def _damage_index_or_journal(
    damage: str, path: Path, sidecar: Path, other: list[_Document], cut: int
) -> None:
    """Apply one scheduled crash or cache failure to a journal and its sidecars."""
    if damage == "drop_sidecar":
        sidecar.unlink(missing_ok=True)
    elif damage == "cut_sidecar" and sidecar.exists():
        sidecar.write_bytes(sidecar.read_bytes()[: cut % (sidecar.stat().st_size + 1)])
    elif damage == "garbage_sidecar":
        with sidecar.open("ab") as stream:
            stream.write(b'[1,2,3\nnot json\n["x"]\n')
    elif damage == "damage_source_stamp":
        path.with_name(path.name + ".spans.source").write_bytes(b"not json")
    elif damage == "tear_journal" and path.exists():
        path.write_bytes(path.read_bytes()[: max(0, path.stat().st_size - cut % 200)])
    elif damage == "other_run" and other:
        elsewhere = path.parent / "elsewhere"
        elsewhere.mkdir()
        _write(elsewhere / RUN_EVENTS_FILE, other)
        sidecar.write_bytes(sidecar_path(elsewhere / RUN_EVENTS_FILE).read_bytes())


@_SLOW
@given(
    documents=_journals(),
    other=_journals(nonce="other"),
    crash=st.tuples(st.integers(0, 40), st.sampled_from(_DAMAGES), st.integers(1, 10_000)),
)
def test_whatever_a_crash_leaves_beside_the_journal_the_answer_is_a_full_scans(
    tmp_path_factory: pytest.TempPathFactory,
    documents: list[_Document],
    other: list[_Document],
    crash: tuple[int, str, int],
) -> None:
    """The run dies after ``crash_after`` events with ``damage`` done beside the journal."""
    crash_after, damage, cut = crash
    directory = tmp_path_factory.mktemp("run")
    path = directory / RUN_EVENTS_FILE
    journal = _Journal.open(path)
    for document in documents[:crash_after]:
        journal.append(document)
    sidecar = sidecar_path(path)
    _damage_index_or_journal(damage, path, sidecar, other, cut)

    # The reader is right at the moment of the crash...
    if path.exists():
        assert _entries(load_journal_index(path)) == _entries(_scan(path))
    # ...and after the writer resumes and the run goes on.
    journal = journal.resume()
    survived = (
        {document["sequence"] for document in _documents_of(path)} if path.exists() else set()
    )
    for document in documents[crash_after:]:
        journal.append(document)
    if path.exists():
        assert _entries(load_journal_index(path)) == _entries(_scan(path))
        assert {d["sequence"] for d in _documents_of(path)} >= survived
        # The sidecar the writer kept is the one a fresh open of the finished journal builds.
        kept = sidecar.read_bytes() if sidecar.exists() else b""
        SpanIndexLog(path)
        assert (sidecar.read_bytes() if sidecar.exists() else b"") == kept


@_SLOW
@given(documents=_journals(), rewritten=_journals(nonce="rewritten"))
def test_a_journal_replaced_under_its_sidecar_is_read_as_it_now_is(
    tmp_path_factory: pytest.TempPathFactory, documents: list[_Document], rewritten: list[_Document]
) -> None:
    path = tmp_path_factory.mktemp("run") / RUN_EVENTS_FILE
    _write(path, documents)
    path.write_bytes(b"".join(_line(document) + b"\n" for document in rewritten))

    assert _entries(load_journal_index(path)) == _entries(_scan(path))


def test_an_in_place_same_size_rewrite_cannot_make_a_span_look_empty(
    tmp_path: Path,
) -> None:
    """A changed middle with matching endpoints invalidates the sidecar before an empty reply."""
    original = [
        _turn_event(1, "e1", "op:a", "first"),
        _span_event(2, "op:a", None, None),
        _turn_event(3, "e1", "op:a", "middle"),
        _turn_event(4, "e1", "op:a", "last"),
    ]
    rewritten = [
        original[0],
        _span_event(2, "op:b", None, None),
        _turn_event(3, "e2", "op:b", "middle"),
        original[3],
    ]
    project = _project_with_journal(tmp_path, original)
    journal = Project.log_directory_for(project.resolve(), _RUN_ID) / RUN_EVENTS_FILE
    SpanIndexLog(journal)
    original_bytes = journal.read_bytes()
    rewritten_bytes = b"".join(_line(document) + b"\n" for document in rewritten)
    assert len(rewritten_bytes) == len(original_bytes)
    assert rewritten_bytes.splitlines()[0] == original_bytes.splitlines()[0]
    assert rewritten_bytes.splitlines()[-1] == original_bytes.splitlines()[-1]
    journal.write_bytes(rewritten_bytes)

    reply = _ask(
        RunsEventsForSpanRequest(
            request_id="s",
            deadline_ms=5000,
            project=str(project),
            run_id=_RUN_ID,
            span_id="op:b",
            max=10,
        )
    )

    assert isinstance(reply, RunsEventsForSpanReply)
    assert reply.total == 1
    assert [event["sequence"] for event in reply.events] == [3]


def test_a_valid_last_line_without_its_newline_is_indexed_and_the_run_goes_on(
    tmp_path: Path,
) -> None:
    path = tmp_path / RUN_EVENTS_FILE
    _write(path, [_turn_event(1, "e1", None, "a"), _turn_event(2, "e1", None, "b")])
    path.write_bytes(path.read_bytes().removesuffix(b"\n"))

    assert [entry.sequence for entry in load_journal_index(path).entries] == [1, 2]

    _Journal.open(path).resume().append(_turn_event(3, "e1", None, "c"))

    index = load_journal_index(path)
    assert [entry.sequence for entry in index.entries] == [1, 2, 3]
    assert _entries(index) == _entries(_scan(path))


def test_the_first_line_that_is_not_an_event_ends_the_indexed_journal(
    tmp_path: Path,
) -> None:
    for damage in (b"not json", b'{"sequence":"3"}', b"", b"[1]", b'{"text":"no sequence"}'):
        path = tmp_path / f"{len(damage)}-{RUN_EVENTS_FILE}"
        events = [_turn_event(1, "e1", None, "a"), _turn_event(2, "e1", None, "b")]
        path.write_bytes(
            b"".join(_line(e) + b"\n" for e in events)
            + damage
            + b"\n"
            + _line(_turn_event(9, "e1", None, "after"))
            + b"\n"
        )

        assert [entry.sequence for entry in load_journal_index(path).entries] == [1, 2]


def test_a_journal_a_resumed_run_restarted_reads_with_the_cursor_the_event_store_gives_it(
    tmp_path: Path,
) -> None:
    path = tmp_path / RUN_EVENTS_FILE
    _write(path, [_turn_event(n, "e1", None, str(n)) for n in (1, 2, 3, 1, 2, 7, 7)])

    entries = load_journal_index(path).entries

    assert [entry.sequence for entry in entries] == [1, 2, 3, 4, 5, 7, 8]
    assert [d["sequence"] for d in read_documents(path, entries)] == [1, 2, 3, 4, 5, 7, 8]


def test_every_new_request_is_gated_by_its_own_feature() -> None:
    gated = {
        RunsEventsForSpanRequest: HelperFeature.RUN_SPAN_EVENTS,
        RunsReadTailRequest: HelperFeature.RUN_TAIL,
    }
    for request_type, feature in gated.items():
        assert HELPER_REQUEST_FEATURES[request_type] is feature
    assert request_features(
        RunsReadTailRequest(request_id="r", deadline_ms=1000, project="/p", run_id=_RUN_ID, tail=3)
    ) == (HelperFeature.RUN_TAIL,)


def _ask(request: HelperRequest) -> HelperReply | HelperError:
    threads = SimThreads()
    host = fake_host(threads, SimNetwork(threads))
    return answer(request, host, threads.event())


def _project_with_journal(root: Path, documents: list[_Document]) -> Path:
    project = project_with_run(root / "proj", _RUN_ID)
    journal = Project.log_directory_for(project.resolve(), _RUN_ID) / RUN_EVENTS_FILE
    journal.parent.mkdir(parents=True, exist_ok=True)
    _write(journal, documents)
    return project


@_SLOW
@given(documents=_journals(), count=st.integers(1, 5))
def test_the_helper_answers_span_and_tail_pages_that_cover_the_journal_exactly(
    tmp_path_factory: pytest.TempPathFactory, documents: list[_Document], count: int
) -> None:
    project = _project_with_journal(tmp_path_factory.mktemp("host"), documents)
    documents = _as_read(documents)

    for span in _SPANS:
        from_sequence = 1
        read: list[_Document] = []
        for _page in range(len(documents) + 2):
            reply = _ask(
                RunsEventsForSpanRequest(
                    request_id="s",
                    deadline_ms=5000,
                    project=str(project),
                    run_id=_RUN_ID,
                    span_id=span,
                    from_sequence=from_sequence,
                    max=count,
                ),
            )
            assert isinstance(reply, RunsEventsForSpanReply)
            read.extend(reply.events)
            assert reply.total == len(_owned_by(documents, span))
            if reply.complete:
                break
            from_sequence = reply.next_sequence
        assert read == _owned_by(documents, span)

    newest_first: list[list[_Document]] = []
    before: int | None = None
    for _page in range(len(documents) + 2):
        tail = _ask(
            RunsReadTailRequest(
                request_id="t",
                deadline_ms=5000,
                project=str(project),
                run_id=_RUN_ID,
                before_sequence=before,
                tail=count,
            ),
        )
        assert isinstance(tail, RunsReadTailReply)
        assert tail.last_sequence == (documents[-1]["sequence"] if documents else 0)
        newest_first.append(list(tail.events))
        if tail.complete:
            break
        oldest = tail.events[0]["sequence"]
        assert isinstance(oldest, int)
        before = oldest
    assert [d for page in reversed(newest_first) for d in page] == documents


@_SLOW
@given(
    batches=st.lists(_journals(), min_size=1, max_size=3),
    replacement=st.none() | _journals(nonce="replaced"),
)
def test_a_helper_that_keeps_indexes_answers_from_the_journal_as_it_now_is(
    tmp_path_factory: pytest.TempPathFactory,
    batches: list[list[_Document]],
    replacement: list[_Document] | None,
) -> None:
    """One long-lived helper, a journal that grows between requests and may be replaced."""
    project = project_with_run(tmp_path_factory.mktemp("host") / "proj", _RUN_ID)
    path = Project.log_directory_for(project.resolve(), _RUN_ID) / RUN_EVENTS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    projects = FileProjects(FakeGitRepositories().repository, SimThreads())
    path.touch()  # a run that journaled nothing yet has an empty journal, not none
    journal = _Journal.open(path)
    written: list[_Document] = []

    def check() -> None:
        seen = _as_read(written)
        tail = projects.run_tail(project, _RUN_ID, None, RUN_EVENTS_READ_MAX)
        assert list(tail.events) == seen[-RUN_EVENTS_READ_MAX:]
        assert tail.last_sequence == (seen[-1]["sequence"] if seen else 0)
        for span in _SPANS:
            page = projects.run_span_events(project, _RUN_ID, span, 1, RUN_EVENTS_READ_MAX)
            assert list(page.events) == _owned_by(seen, span)
            assert page.total == len(page.events)

    for batch in batches:
        for document in batch:
            journal.append(document)
            written.append(document)
        check()
    if replacement is not None:
        path.write_bytes(b"".join(_line(document) + b"\n" for document in replacement))
        written[:] = replacement
        check()


def test_a_span_the_journal_never_names_has_no_events_and_an_unknown_run_is_refused(
    tmp_path: Path,
) -> None:
    project = _project_with_journal(tmp_path, [_turn_event(1, "e1", "op:a", "x")])

    nothing = _ask(
        RunsEventsForSpanRequest(
            request_id="s",
            deadline_ms=5000,
            project=str(project),
            run_id=_RUN_ID,
            span_id="op:nobody",
            max=5,
        ),
    )
    refused = _ask(
        RunsReadTailRequest(
            request_id="t",
            deadline_ms=5000,
            project=str(project),
            run_id="20261010-120000-22222222-other",
            tail=5,
        ),
    )

    assert nothing == RunsEventsForSpanReply(
        request_id="s", events=(), next_sequence=1, complete=True, total=0
    )
    assert isinstance(refused, HelperError)
    assert refused.code is HelperErrorCode.UNKNOWN_RUN


def test_a_run_with_an_empty_journal_has_an_empty_tail(tmp_path: Path) -> None:
    project = _project_with_journal(tmp_path, [])

    tail = _ask(
        RunsReadTailRequest(
            request_id="t", deadline_ms=5000, project=str(project), run_id=_RUN_ID, tail=5
        ),
    )

    assert tail == RunsReadTailReply(request_id="t", events=(), complete=True, last_sequence=0)


def test_a_tail_page_stops_at_its_byte_bound_and_the_pages_still_cover_the_journal(
    tmp_path: Path,
) -> None:
    # Each event is most of a helper line, so a few of them pass the page's byte bound.
    size = HELPER_LINE_MAX_BYTES // 2
    sequences = list(range(1, 2 * (RUN_EVENTS_READ_MAX_BYTES // size) + 3))
    project = _project_with_journal(
        tmp_path, [_turn_event(n, "e1", None, "x" * size) for n in sequences]
    )

    pages: list[RunsReadTailReply] = []
    before: int | None = None
    while True:
        reply = _ask(
            RunsReadTailRequest(
                request_id="t",
                deadline_ms=5000,
                project=str(project),
                run_id=_RUN_ID,
                before_sequence=before,
                tail=RUN_EVENTS_READ_MAX,
            ),
        )
        assert isinstance(reply, RunsReadTailReply)
        pages.append(reply)
        if reply.complete:
            break
        oldest = reply.events[0]["sequence"]
        assert isinstance(oldest, int)
        before = oldest

    assert len(pages) > 1
    for page in pages:
        assert len(page.events) >= 1
        assert len(page.events) == 1 or sum(len(_line(e)) + 1 for e in page.events) <= (
            RUN_EVENTS_READ_MAX_BYTES
        )
    covered = [e["sequence"] for page in reversed(pages) for e in page.events]
    assert covered == sequences


@settings(max_examples=examples(pr=4, nightly=30), deadline=None)
@given(
    turns=st.integers(1, 3),
    order=st.randoms(use_true_random=False),
    words=st.lists(st.booleans(), min_size=0, max_size=6),
)
def test_a_real_runs_spans_read_back_as_the_journal_filtered_by_span(
    tmp_path_factory: pytest.TempPathFactory, turns: int, order: Random, words: list[bool]
) -> None:
    """The server's own writer keeps the sidecar level, and each turn's events come back by span."""
    parts = build_server_parts()
    directory = tmp_path_factory.mktemp("run")
    run = run_to_end(parts, directory, turns, order)
    spans = [span for span in run.trace().spans if span.invocation is not None]
    for span in spans:
        for number, kind in enumerate(words):
            parts.core_events.emit(
                CoreEventType.TOOL_CALL if kind else CoreEventType.AGENT_OUTPUT_CHUNK,
                execution_id=span.invocation,
                data=ToolCallData(tool="Read", call_id=f"c{number}", args={})
                if kind
                else AgentOutputChunkData(channel="assistant", content=f"w{number}"),
            )
    parts.core_events.emit(
        CoreEventType.AGENT_OUTPUT_CHUNK,
        execution_id="no-such-invocation",
        data=AgentOutputChunkData(channel="assistant", content="stray"),
    )
    path = directory / RUN_EVENTS_FILE
    documents = _documents_of(path)

    index = load_journal_index(path)
    assert _entries(index) == _entries(_scan(path))
    for span in [*(s.id for s in run.trace().spans), "op:nobody"]:
        entries, total, complete = span_page(index, span, 1, 10_000, 1 << 30)
        owned = _owned_by(documents, span)
        assert complete
        assert total == len(entries) == len(owned)
    for span in spans:
        assert len(_owned_by(documents, span.id)) == len(words)
