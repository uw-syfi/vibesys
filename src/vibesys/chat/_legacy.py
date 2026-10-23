"""Conversations recorded before sessions, carried into the canonical session logs.

A thread of the preceding app lives in the run's server journal (``run-events.jsonl``): a
``chat_thread_created`` event and one ``chat`` event per exchange, the question in ``text`` and
the answer in ``data.answer``. Reading sessions only from ``sessions/*/session.log`` would make
every such conversation disappear, so the import writes each thread as the session it would have
been: a creation record, then a user and an assistant message per exchange. The journal is never
changed, and a thread that already has a committed session is left as it is, so the import is
idempotent and never overwrites a conversation continued after it.
"""

from __future__ import annotations

import json
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime
from itertools import islice
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

from vibesys.chat._session import ChatRuntime, run_head, session_head, text_message
from vs_project.api import Project
from vs_proto.api import (
    ChatRecord,
    ChatRole,
    ChatSessionCreated,
    Oneof,
    Record,
    RunHead,
    Timestamp,
    validate,
)
from vs_runlog.api import FileRunContent, FileRunLogWriter, RunLock, RunLogReader

if TYPE_CHECKING:
    from collections.abc import Iterator

JOURNAL = "run-events.jsonl"
DEFAULT_THREAD = "default"
# The journal records no harness for the implicit first thread; the session needs a name.
UNRECORDED = "unrecorded"
_TITLE_CHARS = 120


@dataclass(frozen=True)
class _Exchange:
    request_id: str
    question: str
    answer: str
    at: float


@dataclass
class _Thread:
    provider: str = UNRECORDED
    model: str = UNRECORDED
    title: str = ""
    created_at: float | None = None

    def __post_init__(self) -> None:
        self.exchanges: list[_Exchange] = []


def _seconds(value: object) -> float:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).timestamp()
        except ValueError:
            return 0.0
    return 0.0


def _events(path: Path) -> Iterator[dict[str, object]]:
    with path.open("rb") as stream:
        for line in stream:
            if b'"chat' not in line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("type") in {"chat", "chat_thread_created"}:
                yield event


def legacy_threads(journal: Path) -> dict[str, _Thread]:
    """The threads a server journal records, by id, in the order they first appear."""
    threads: dict[str, _Thread] = {}
    for event in _events(journal):
        thread_id = event.get("chat_thread_id")
        thread_id = thread_id if isinstance(thread_id, str) and thread_id else DEFAULT_THREAD
        thread = threads.setdefault(thread_id, _Thread())
        data = event.get("data")
        data = data if isinstance(data, dict) else {}
        if event["type"] == "chat_thread_created":
            thread.provider = str(data.get("provider") or UNRECORDED)
            thread.model = str(data.get("model") or UNRECORDED)
            thread.title = str(data.get("title") or "")
            thread.created_at = _seconds(data.get("created_at") or event.get("timestamp"))
            continue
        question, answer = event.get("text"), data.get("answer")
        if not isinstance(question, str) or not isinstance(answer, str):
            continue
        title = data.get("thread_title")
        if isinstance(title, str) and title and not thread.title:
            thread.title = title
        sequence = event.get("sequence")
        request_id = f"legacy-{sequence if isinstance(sequence, int) else len(thread.exchanges)}"
        thread.exchanges.append(
            _Exchange(request_id, question, answer, _seconds(event.get("timestamp")))
        )
    return threads


def _stamp(seconds: float) -> Timestamp:
    whole = int(seconds)
    return Timestamp(seconds=whole, nanos=int((seconds - whole) * 1e9))


def _committed(directory: Path, records: int) -> bool:
    head_path = directory / "run.head"
    if not head_path.exists():
        return False
    head = RunHead.from_binary(head_path.read_bytes())
    validate(head)
    log = directory / "session.log"
    if not log.exists():
        return False
    if head.committed_seq >= records:
        return True
    with log.open("rb") as stream:
        for record in islice(RunLogReader(stream), head.committed_seq):
            body = record.body
            event = body.value.event if body is not None and body.field == "chat" else None
            if (
                event is not None
                and event.field == "message"
                and not event.value.request_id.startswith("legacy-")
            ):
                return True
    return False


def _write(
    directory: Path, thread_id: str, thread: _Thread, epoch: int, runtime: ChatRuntime
) -> None:
    content = FileRunContent(directory.parent / "blobs", directory.parent / "streams")
    with FileRunLogWriter(
        directory / "session.log",
        clock=runtime.clock,
        threads=runtime.threads,
        headline=session_head,
    ) as writer:

        def append(event: ChatRecord, at: float) -> None:
            writer.append(
                Record(
                    seq=writer.sequence + 1,
                    epoch=epoch,
                    at=_stamp(at),
                    body=Oneof("chat", event),
                )
            )

        first = thread.created_at or (thread.exchanges[0].at if thread.exchanges else 0.0)
        created = ChatSessionCreated(
            id=thread_id,
            title=thread.title[:_TITLE_CHARS],
            provider=thread.provider,
            model=thread.model,
        )
        append(ChatRecord(event=Oneof("created", created)), first)
        for exchange in thread.exchanges:
            for role, text in (
                (ChatRole.USER, exchange.question),
                (ChatRole.ASSISTANT, exchange.answer),
            ):
                message = text_message(content, exchange.request_id, role, text)
                append(ChatRecord(event=Oneof("message", message)), exchange.at)
        writer.commit()


def import_legacy_chat(project_root: str, run_id: str, runtime: ChatRuntime) -> tuple[str, ...]:
    """Write every journal thread that has no committed session yet; the ids written."""
    journal = Project.log_directory_for(project_root, run_id) / JOURNAL
    if not journal.exists():
        return ()
    epoch, _ = run_head(Project.run_head_path_for(project_root, run_id))
    written = []
    for thread_id, thread in legacy_threads(journal).items():
        directory = Project.chat_session_directory_for(project_root, run_id, thread_id)
        directory.mkdir(parents=True, exist_ok=True)
        with ExitStack() as locks:
            gate = RunLock(directory / "admission.lock")
            locks.callback(gate.close)
            if _committed(directory, 1 + 2 * len(thread.exchanges)):
                continue
            writer_gate = RunLock(directory / "run.lock")
            locks.callback(writer_gate.close)
            with TemporaryDirectory(prefix=".legacy-import-", dir=directory) as temporary:
                staged = Path(temporary)
                _write(staged, thread_id, thread, epoch, runtime)
                # The log is complete before it becomes visible; publish its commit fence last.
                (staged / "session.log").replace(directory / "session.log")
                (staged / "run.head").replace(directory / "run.head")
            written.append(thread_id)
    return tuple(written)
