"""Conversations of the preceding app reach the session logs, whatever shape their journal has."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st
from tests.vibesys.api.test_chat_sessions import chat_project

from vibesys.api.chat import ChatRuntime, import_legacy_chat
from vs_project.api import Project
from vs_proto.api import ChatMessage, ChatRole, RunHead, validate
from vs_runlog.api import RunLogReader, encode_frame
from vs_sim.api.testing import SimThreads

TEXT = st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=40)
BIG = "x" * 70_000


@st.composite
def journals(draw: st.DrawFn) -> list[dict[str, object]]:
    """Chat events as the server journal wrote them, interleaved with everything else it wrote."""
    ids = draw(st.lists(st.sampled_from(["default", "a1", "b2", "c3"]), unique=True, max_size=4))
    events: list[dict[str, object]] = []
    sequence = 0
    for thread in ids:
        if thread != "default" and draw(st.booleans()):
            sequence += 1
            events.append(
                {
                    "sequence": sequence,
                    "timestamp": "2026-01-01T00:00:00+00:00",
                    "type": "chat_thread_created",
                    "chat_thread_id": thread,
                    "data": {
                        "kind": "chat_thread_created",
                        "thread_id": thread,
                        "title": draw(TEXT),
                        "provider": draw(st.sampled_from(["claude", "codex"])),
                        "model": draw(st.sampled_from(["opus", "gpt"])),
                        "created_at": "2026-01-01T00:00:00+00:00",
                    },
                }
            )
    for _ in range(draw(st.integers(0, 8))):
        sequence += 1
        thread = draw(st.sampled_from(ids or ["default"]))
        events.append(
            {
                "sequence": sequence,
                "timestamp": "2026-01-01T00:00:01+00:00",
                "type": draw(st.sampled_from(["chat", "chat", "agent_output_chunk"])),
                "text": draw(TEXT),
                "chat_thread_id": None if thread == "default" else thread,
                "data": {
                    "kind": "chat",
                    "answer": draw(st.sampled_from([BIG, "ok", "ok", "ok", "答"])),
                },
            }
        )
    return events


def journal_bytes(events: list[dict[str, object]], *, noise: bool) -> bytes:
    lines = [json.dumps(event, ensure_ascii=False) for event in events]
    if noise:
        lines.insert(len(lines) // 2, 'not json at all with "chat in it')
    return ("\n".join(lines) + "\n").encode()


def expected(events: list[dict[str, object]]) -> dict[str, list[tuple[str, str]]]:
    """Each thread's exchanges in journal order, exactly as the journal gave them."""
    threads: dict[str, list[tuple[str, str]]] = {}
    for event in events:
        thread = str(event.get("chat_thread_id") or "default")
        if event["type"] == "chat_thread_created":
            threads.setdefault(thread, [])
        elif event["type"] == "chat":
            data = event["data"]
            assert isinstance(data, dict)
            threads.setdefault(thread, []).append((str(event["text"]), str(data["answer"])))
    return threads


def session(root: str, run_id: str, thread: str) -> list:
    directory = Project.chat_session_directory_for(root, run_id, thread)
    with (directory / "session.log").open("rb") as stream:
        records = list(RunLogReader(stream))
    head = RunHead.from_binary((directory / "run.head").read_bytes())
    validate(head)
    assert head.committed_seq == len(records)
    for record in records:
        validate(record)
    return records


def text_of(root: str, run_id: str, thread: str, message: ChatMessage) -> str:
    assert message.content is not None
    if message.content.field == "text":
        return message.content.value
    directory = Project.chat_session_directory_for(root, run_id, thread)
    return (directory / "blobs" / message.content.value.sha256).read_text()


@pytest.mark.regression
@example(
    events=[
        {
            "sequence": 1,
            "timestamp": "2026-01-01T00:00:00+00:00",
            "type": "chat",
            "text": "why?",
            "chat_thread_id": None,
            "data": {"kind": "chat", "answer": "because"},
        }
    ],
    noise=True,
)
@settings(deadline=None, max_examples=12)
@given(events=journals(), noise=st.booleans())
def test_every_journal_conversation_becomes_one_session_with_its_exchanges(
    events: list[dict[str, object]], *, noise: bool
) -> None:
    """Threads, harnesses, titles and exchanges survive; the import is idempotent and keeps the journal."""
    with TemporaryDirectory() as temporary, pytest.MonkeyPatch.context() as environment:
        environment.setenv("VIBESYS_STATE_HOME", str(Path(temporary) / "state"))
        chat = chat_project(Path(temporary) / "project")
        journal = Project.log_directory_for(chat.project_root, chat.run_id) / "run-events.jsonl"
        data = journal_bytes(events, noise=noise)
        journal.write_bytes(data)
        threads = SimThreads()
        runtime = ChatRuntime(threads.clock, threads)
        written = import_legacy_chat(chat.project_root, chat.run_id, runtime)
        want = expected(events)
        assert sorted(written) == sorted(want)
        for thread, exchanges in want.items():
            records = session(chat.project_root, chat.run_id, thread)
            created = records[0].body.value.event
            assert created.field == "created"
            assert created.value.id == thread
            messages = [
                (
                    r.body.value.event.value.role,
                    text_of(chat.project_root, chat.run_id, thread, r.body.value.event.value),
                )
                for r in records[1:]
            ]
            flat = [
                item
                for question, answer in exchanges
                for item in ((ChatRole.USER, question), (ChatRole.ASSISTANT, answer))
            ]
            assert messages == flat
        before = {
            thread: (
                Project.chat_session_directory_for(chat.project_root, chat.run_id, thread)
                / "session.log"
            ).read_bytes()
            for thread in want
        }
        assert import_legacy_chat(chat.project_root, chat.run_id, runtime) == ()
        for thread, content in before.items():
            path = (
                Project.chat_session_directory_for(chat.project_root, chat.run_id, thread)
                / "session.log"
            )
            assert path.read_bytes() == content
        assert journal.read_bytes() == data


@pytest.mark.regression
@pytest.mark.parametrize(("prefix", "continued"), [(1, False), (2, False), (2, True)])
def test_retry_replaces_every_incomplete_import_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prefix: int, *, continued: bool
) -> None:
    """A crash after creation or either side of an exchange cannot publish a truncated import."""
    monkeypatch.setenv("VIBESYS_STATE_HOME", str(tmp_path / "state"))
    chat = chat_project(tmp_path / "project")
    journal = Project.log_directory_for(chat.project_root, chat.run_id) / "run-events.jsonl"
    data = journal_bytes(
        [{"sequence": 1, "type": "chat", "text": "why?", "data": {"answer": "because"}}],
        noise=False,
    )
    journal.write_bytes(data)
    threads = SimThreads()
    runtime = ChatRuntime(threads.clock, threads)
    import_legacy_chat(chat.project_root, chat.run_id, runtime)
    complete = session(chat.project_root, chat.run_id, "default")
    directory = Project.chat_session_directory_for(chat.project_root, chat.run_id, "default")
    # A durable prefix left by the preceding importer, including its published commit fence.
    if continued:
        complete[1].body.value.event.value.request_id = "native-request"
    (directory / "session.log").write_bytes(
        b"VSRLOG01" + b"".join(encode_frame(r) for r in complete[:prefix])
    )
    head = RunHead.from_binary((directory / "run.head").read_bytes())
    head.committed_seq = prefix
    (directory / "run.head").write_bytes(head.to_binary())
    assert import_legacy_chat(chat.project_root, chat.run_id, runtime) == (
        () if continued else ("default",)
    )
    assert session(chat.project_root, chat.run_id, "default") == (
        complete[:prefix] if continued else complete
    )
    assert journal.read_bytes() == data


@pytest.mark.regression
@pytest.mark.parametrize(
    "state", ["complete log, old head", "complete log, no head", "leftover directory"]
)
def test_retry_after_an_interrupted_publish_reaches_the_full_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    """The log is renamed in before its head, so a crash between or before the renames retries."""
    monkeypatch.setenv("VIBESYS_STATE_HOME", str(tmp_path / "state"))
    chat = chat_project(tmp_path / "project")
    journal = Project.log_directory_for(chat.project_root, chat.run_id) / "run-events.jsonl"
    journal.write_bytes(
        journal_bytes(
            [{"sequence": 1, "type": "chat", "text": "why?", "data": {"answer": "because"}}],
            noise=False,
        )
    )
    threads = SimThreads()
    runtime = ChatRuntime(threads.clock, threads)
    import_legacy_chat(chat.project_root, chat.run_id, runtime)
    complete = session(chat.project_root, chat.run_id, "default")
    directory = Project.chat_session_directory_for(chat.project_root, chat.run_id, "default")
    head_path = directory / "run.head"
    if state == "complete log, no head":
        head_path.unlink()
    else:
        head = RunHead.from_binary(head_path.read_bytes())
        head.committed_seq = 1
        head_path.write_bytes(head.to_binary())
    if state == "leftover directory":
        leftover = directory / ".legacy-import-killed"
        leftover.mkdir()
        (leftover / "session.log").write_bytes(b"VSRLOG01")
        (directory / "session.log").write_bytes(
            b"VSRLOG01" + b"".join(encode_frame(r) for r in complete[:1])
        )
    assert import_legacy_chat(chat.project_root, chat.run_id, runtime) == ("default",)
    assert session(chat.project_root, chat.run_id, "default") == complete
    assert import_legacy_chat(chat.project_root, chat.run_id, runtime) == ()
