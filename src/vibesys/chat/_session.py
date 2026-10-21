"""Durable chat turns over the existing auxiliary conversation and run-log writer."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import ValidationError

from vibesys.api.auxiliary import STEER_PROPOSAL_KIND, SteerProposalReceipt
from vibesys.chat._lifecycle import SessionAction, SessionLifecycle
from vibesys.events import AgentOutputChunkData, CoreEvent, JsonResultPayload, ToolResultData
from vs_project.api import InboxEntry, Project
from vs_proto.api import (
    BlobRef,
    ChatMessage,
    ChatMessageRequest,
    ChatRecord,
    ChatRole,
    ChatSessionCreated,
    ChatSteerProposed,
    ChatToken,
    Keyframe,
    Lifecycle,
    Oneof,
    Record,
    RunHead,
    Running,
    SteerMode,
    SteerTarget,
    SteerTargetKind,
    Timestamp,
    validate,
)
from vs_proto.api import (
    ValidationError as ProtoValidationError,
)
from vs_runlog.api import FileRunContent, FileRunLogWriter, RunLock

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vibesys.api.auxiliary import ManagedAgent
    from vs_proto.api import RunProcess
    from vs_sim.api import SleepingClock, Threads


INLINE_BYTES = 65536


@dataclass(frozen=True)
class ChatRuntime:
    clock: SleepingClock
    threads: Threads
    idle_seconds: float = 300.0
    poll_seconds: float = 0.1


def session_head(state: Keyframe, process: RunProcess | None) -> RunHead:
    """Use the writer's ordinary commit fence without inventing a run epoch."""
    del process
    return RunHead(
        strategy="chat",
        lifecycle=Lifecycle(state=Oneof("running", Running())),
        committed_seq=state.through_seq,
    )


def run_head(path: Path) -> tuple[int, bool]:
    """Read the run's published lifecycle, with no write access to run storage."""
    head = RunHead.from_binary(path.read_bytes())
    validate(head)
    if head.process is None or head.lifecycle is None or head.lifecycle.state is None:
        message = "chat requires a native run.head with process and lifecycle"
        raise ValueError(message)
    return head.process.epoch, head.lifecycle.state.field == "parked"


class ChatSession:
    """One process owns one session log; turn completion precedes inbox settlement."""

    def __init__(self, request: ChatMessageRequest, runtime: ChatRuntime) -> None:
        validate(request)
        self.request = request
        self.runtime = runtime
        self.project = Project.open(request.project_root)
        self.project.state.require_local_run(request.run_id)
        self.inbox = self.project.state.session_inbox(request.run_id, request.session_id)
        self.directory = Project.chat_session_directory_for(
            request.project_root, request.run_id, request.session_id
        )
        self.directory.mkdir(parents=True, exist_ok=True)
        self.run_directory = Project.run_log_path_for(request.project_root, request.run_id).parent
        self.writer = FileRunLogWriter(
            self.directory / "session.log",
            clock=runtime.clock,
            threads=runtime.threads,
            headline=session_head,
        )
        self._lock = runtime.threads.rlock()
        self._current: str | None = None
        self._epoch = 1
        self._restoring = True
        self._content = FileRunContent(self.directory / "blobs", self.directory / "streams")

    def close(self) -> None:
        """Drain the shared writer and release the process's lifetime lock."""
        self.writer.close()

    def history(self) -> str:
        """Restore complete exchanges as untrusted data, using the authoritative log."""
        questions: dict[str, str] = {}
        exchanges: list[dict[str, str]] = []
        for record in self.writer.read():
            if record.body is None or record.body.field != "chat":
                continue
            event = record.body.value.event
            if event is None or event.field != "message":
                continue
            message = event.value
            text = self._text(message)
            if message.role == ChatRole.USER:
                questions[message.request_id] = text
            elif not message.failed and message.request_id in questions:
                exchanges.append({"question": questions[message.request_id], "answer": text})
        # Complete records only; bounded restoration keeps the provider's context finite.
        lines: list[str] = []
        used = 0
        for exchange in reversed(exchanges):
            line = json.dumps(exchange, ensure_ascii=False)
            used += len(line.encode()) + 1
            if used > INLINE_BYTES:
                break
            lines.append(line)
        return "\n".join(reversed(lines))

    def serve(self, created: ChatSessionCreated, open_agent: Callable[[], ManagedAgent]) -> None:
        """Start on demand; finish the current turn, then retire at park or idle."""
        epoch, parked = run_head(self.run_directory / "run.head")
        self._epoch = epoch
        state = SessionLifecycle(self.runtime.clock.now(), epoch, self.runtime.idle_seconds)
        if self.writer.sequence == 0:
            self._append(ChatRecord(event=Oneof("created", created)))
            self.writer.commit()
        self._recover_settlement()
        self.inbox.serve()
        agent: ManagedAgent | None = None
        try:
            while True:
                # Admission and final close share the same lock as the poster. A post
                # cannot observe a live owner which has already decided to leave.
                try:
                    gate = RunLock(self.directory / "admission.lock")
                except BlockingIOError:
                    self.runtime.threads.sleep(self.runtime.poll_seconds)
                    continue
                try:
                    epoch, parked = run_head(self.run_directory / "run.head")
                    pending = self.inbox.pending()
                    action = state.next(
                        self.runtime.clock.now(), epoch, parked=parked, pending=bool(pending)
                    )
                    if action is SessionAction.EXIT:
                        self.close()
                        return
                finally:
                    gate.close()
                if action is SessionAction.WAIT:
                    self.runtime.threads.sleep(self.runtime.poll_seconds)
                    continue
                entry = pending[0]
                question = self._question(entry)
                if question is None:
                    continue
                agent = self._answer(agent, open_agent, question)
                state = state.answered(self.runtime.clock.now())
        finally:
            if agent is not None:
                agent.close()

    def _question(self, entry: InboxEntry) -> ChatMessageRequest | None:
        if entry.unreadable is not None:
            self.inbox.reject(entry.request_id, entry.unreadable)
            return None
        try:
            question = ChatMessageRequest.from_binary(entry.payload)
            validate(question)
        except (ValueError, ProtoValidationError) as error:
            self.inbox.reject(entry.request_id, str(error))
            return None
        if (
            question.project_root,
            question.run_id,
            question.session_id,
            question.request_id,
        ) != (
            self.request.project_root,
            self.request.run_id,
            self.request.session_id,
            entry.request_id,
        ):
            self.inbox.reject(entry.request_id, "chat message routing does not match its inbox")
            return None
        return question

    def observe(self, event: CoreEvent) -> None:
        """Consume the same streamed events and proposal receipts as legacy chat."""
        with self._lock:
            if self._current is None or event.execution_id != self._current:
                return
            self._observe(event)

    def _observe(self, event: CoreEvent) -> None:
        data = event.data
        if isinstance(data, AgentOutputChunkData) and data.channel == "assistant":
            self.token(data.content)
        elif (
            isinstance(data, ToolResultData)
            and not data.is_error
            and isinstance(data.payload, JsonResultPayload)
        ):
            value = data.payload.value
            if not isinstance(value, dict) or value.get("kind") != STEER_PROPOSAL_KIND:
                return
            try:
                receipt = SteerProposalReceipt.model_validate(value)
            except ValidationError:
                return
            proposal = receipt.proposal
            identity = (
                hashlib.sha256(f"{event.execution_id}:{data.call_id}".encode()).hexdigest()
                if data.call_id
                else f"proposal-{self.writer.sequence + 1}"
            )
            self.propose(
                ChatSteerProposed(
                    id=identity,
                    text=proposal.text,
                    target=SteerTarget(
                        kind=SteerTargetKind[proposal.target.kind.upper()],
                        id=proposal.target.id,
                    ),
                    mode=SteerMode[proposal.mode.upper()],
                )
            )

    def token(self, text: str) -> None:
        """Append streamed assistant output under the current request's identity."""
        with self._lock:
            if self._current is None:
                return
            # Slice encoded bytes on Unicode boundaries, never truncate content.
            for start in range(0, len(text), 16000):
                self._append(
                    ChatRecord(
                        event=Oneof(
                            "token",
                            ChatToken(request_id=self._current, text=text[start : start + 16000]),
                        )
                    )
                )

    def propose(self, proposal: ChatSteerProposed) -> None:
        """Record a proposal, with no capability for posting a run intent."""
        with self._lock:
            if self._current is not None:
                proposal.request_id = self._current
                self._append(ChatRecord(event=Oneof("proposed_steer", proposal)))

    def _answer(
        self,
        agent: ManagedAgent | None,
        open_agent: Callable[[], ManagedAgent],
        question: ChatMessageRequest,
    ) -> ManagedAgent | None:
        with self._lock:
            self._current = question.request_id
        self._append(
            ChatRecord(
                event=Oneof(
                    "message", self._message(question.request_id, ChatRole.USER, question.text)
                )
            )
        )
        self.writer.commit()
        try:
            history = self.history() if self._restoring else ""
            self._restoring = False
            prompt = (
                question.text
                if not history
                else json.dumps(
                    {
                        "untrusted_prior_chat_transcript_jsonl": history,
                        "question": question.text,
                    },
                    ensure_ascii=False,
                )
            )
            try:
                if agent is None:
                    agent = open_agent()
                answer = agent.turn(prompt, invocation_id=question.request_id)
                message = self._message(question.request_id, ChatRole.ASSISTANT, answer)
            except (RuntimeError, ValueError, OSError) as error:
                message = self._message(question.request_id, ChatRole.ASSISTANT, str(error))
                message.failed = True
            self._append(ChatRecord(event=Oneof("message", message)))
            self.writer.commit()
            self.inbox.settle(question.request_id)
            return agent
        finally:
            with self._lock:
                self._current = None

    def _recover_settlement(self) -> None:
        for record in self.writer.read():
            if record.body and record.body.field == "chat":
                event = record.body.value.event
                if event and event.field == "message" and event.value.role == ChatRole.ASSISTANT:
                    self.inbox.settle(event.value.request_id)

    def _message(self, request_id: str, role: ChatRole, text: str) -> ChatMessage:
        content = text.encode()
        if len(content) <= INLINE_BYTES:
            return ChatMessage(request_id=request_id, role=role, content=Oneof("text", text))
        digest = hashlib.sha256(content).hexdigest()
        self._content.write_blob(digest, content)
        return ChatMessage(
            request_id=request_id,
            role=role,
            content=Oneof(
                "blob",
                BlobRef(sha256=digest, size=len(content), media_type="text/plain; charset=utf-8"),
            ),
        )

    def _text(self, message: ChatMessage) -> str:
        if message.content is None:
            return ""
        if message.content.field == "text":
            return message.content.value
        return (self.directory / "blobs" / message.content.value.sha256).read_text()

    def _append(self, event: ChatRecord) -> None:
        with self._lock:
            now = self.runtime.clock.now()
            seconds = int(now)
            self.writer.append(
                Record(
                    seq=self.writer.sequence + 1,
                    epoch=self._epoch,
                    at=Timestamp(seconds=seconds, nanos=int((now - seconds) * 1e9)),
                    body=Oneof("chat", event),
                )
            )
