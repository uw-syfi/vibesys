"""Frames to items: the deterministic fold from agentshim events to sidecar events.

The same events in the same order always give the same output, and a repeated event changes
nothing, so replaying a raw log reproduces the event log. Nothing here reads a clock, a file or a
socket; the engine feeds events in and appends what comes out.

Output events carry ``offset == 0``: the store numbers them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agentshim import (
    AssistantText,
    ProviderUsage,
    Reasoning,
    SteerConsumed,
    TextDelta,
    TextKind,
    ToolCall,
    ToolResult,
    TurnInterrupted,
    UsageReport,
)

from vs_sidecar.gen.vibesys.orchestrator.v1 import orchestrator_pb2 as orch
from vs_sidecar.gen.vibesys.orchestrator.v1 import sidecar_pb2 as sc

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from typing import Any

    from agentshim import AgentEvent


class SteerBook:
    """The steers sent to the running turn and not yet answered, oldest first.

    agentshim names a steer by its text, so two steers with the same text are answered in the
    order they were sent.
    """

    def __init__(self) -> None:
        """Start with no open steer."""
        self._open: list[tuple[str, str]] = []

    def add(self, message_id: str, text: str) -> None:
        """Note a steer sent to the provider."""
        self._open.append((message_id, text))

    def take(self, text: str) -> str | None:
        """The message id of the oldest open steer with *text*, now answered."""
        for index, (message_id, candidate) in enumerate(self._open):
            if candidate == text:
                del self._open[index]
                return message_id
        return None

    def drain(self) -> list[str]:
        """Every message id still open, oldest first, now answered."""
        ids = [message_id for message_id, _ in self._open]
        self._open.clear()
        return ids


@dataclass
class _Item:
    """What is known of one item so far."""

    kind: str  # "assistant" | "reasoning" | "tool"
    status: int
    text: str = ""
    tool: str = ""
    call_id: str = ""
    args_json: str = ""
    output: str = ""
    exit_code: int | None = None
    is_error: bool = False

    def proto(self, item_id: str, turn_id: str) -> sc.SidecarItem:
        """The item as it is now."""
        item = sc.SidecarItem(id=item_id, turn_id=turn_id, status=self.status)  # type: ignore[arg-type]
        if self.kind == "tool":
            tool = orch.ToolCall(
                tool=self.tool or "unknown",
                provider_call_id=self.call_id,
                args_json=self.args_json,
                output=self.output,
                is_error=self.is_error,
            )
            if self.exit_code is not None:
                tool.exit_code = self.exit_code
            item.tool.CopyFrom(tool)
        elif self.kind == "reasoning":
            item.reasoning.CopyFrom(orch.Text(text=self.text))
        else:
            item.assistant.CopyFrom(orch.Text(text=self.text))
        return item


def usage_proto(usage: ProviderUsage) -> sc.Usage:  # type: ignore[name-defined]
    """The usage a provider reported, leaving out what it did not report.

    A usage whose increment is unknown has no counts. Cache and reasoning counts are set only when
    positive, because a provider that has no such count reports zero, and zero would claim it.
    """
    result = orch.Usage()
    if usage.total_cost_usd is not None:
        result.cost_usd = usage.total_cost_usd
    if not usage.increment_known:
        return result
    tokens = usage.tokens
    result.input_tokens = tokens.input_tokens
    result.output_tokens = tokens.output_tokens
    if tokens.cache_read_input_tokens:
        result.cache_read_tokens = tokens.cache_read_input_tokens
    if tokens.cache_write_input_tokens:
        result.cache_creation_tokens = tokens.cache_write_input_tokens
    if tokens.reasoning_output_tokens:
        result.reasoning_tokens = tokens.reasoning_output_tokens
    return result


class TurnNormalizer:
    """Folds the events of one turn into sidecar events.

    ``feed`` returns what one event adds; ``finish`` closes the turn. An event that repeats what
    is already known adds nothing, and a usage report replaces the turn's usage (Codex reports
    totals, so summing would count twice).
    """

    def __init__(self, turn_id: str, steers: SteerBook | None = None) -> None:
        """Fold the events of turn *turn_id*, answering steers from *steers*."""
        self._turn_id = turn_id
        self._steers = steers if steers is not None else SteerBook()
        self._items: dict[str, _Item] = {}
        self._minted = 0
        self._usage: sc.Usage | None = None  # type: ignore[name-defined]
        self._interrupted = False

    @property
    def turn_id(self) -> str:
        """The turn this folds."""
        return self._turn_id

    @property
    def interrupted(self) -> bool:
        """Whether the provider said the turn was interrupted."""
        return self._interrupted

    def feed(self, event: AgentEvent) -> list[sc.SidecarEvent]:
        """What *event* adds to the log, in order."""
        if isinstance(event, (AssistantText, Reasoning)):
            return self._text(event)
        handler = self._handlers().get(type(event))
        if handler is None:
            return []
        return handler(event)

    def _handlers(self) -> dict[type, Callable[[Any], list[sc.SidecarEvent]]]:
        return {
            ToolCall: self._call,
            ToolResult: self._result,
            TextDelta: self._delta,
            UsageReport: lambda event: self._report(event.usage),
            SteerConsumed: lambda event: self._steer(event.text, accepted=True),
            TurnInterrupted: self._interrupted_by,
        }

    def _interrupted_by(self, _event: TurnInterrupted) -> list[sc.SidecarEvent]:
        self._interrupted = True
        return []

    def finish(self, status: int, usage: ProviderUsage | None = None) -> list[sc.SidecarEvent]:
        """Close the turn: settle unfinished items, end it, then answer unanswered steers.

        An item still running when the turn ends (Codex does not always send ``item/completed``)
        is finished if the turn completed and failed otherwise. A steer that was never consumed
        is answered as not accepted, after the ``TurnEnded``, as ``SteerOutcome`` says.
        """
        out: list[sc.SidecarEvent] = []
        settled = (
            orch.ITEM_STATUS_FINISHED
            if status == orch.TURN_STATUS_COMPLETED
            else orch.ITEM_STATUS_FAILED
        )
        for item_id, item in self._items.items():
            if item.status == orch.ITEM_STATUS_RUNNING:
                item.status = settled
                out.append(self._event(item=item.proto(item_id, self._turn_id)))
        if usage is not None:
            out.extend(self._report(usage))
        ended = sc.TurnEnded(turn_id=self._turn_id, status=status)  # type: ignore[arg-type]
        if self._usage is not None:
            ended.usage.CopyFrom(self._usage)
        out.append(self._event(turn_ended=ended))
        out.extend(self._outcome(message_id, accepted=False) for message_id in self._steers.drain())
        return out

    # -- items

    def _mint(self) -> str:
        self._minted += 1
        return f"{self._turn_id}:n{self._minted}"

    def _text(self, event: AssistantText | Reasoning) -> list[sc.SidecarEvent]:
        item_id = event.item_id or self._mint()
        kind = "assistant" if isinstance(event, AssistantText) else "reasoning"
        known = self._items.get(item_id)
        if (
            known is not None
            and known.text == event.text
            and known.status == orch.ITEM_STATUS_FINISHED
        ):
            return []
        item = _Item(kind=kind, status=orch.ITEM_STATUS_FINISHED, text=event.text)
        self._items[item_id] = item
        return [self._event(item=item.proto(item_id, self._turn_id))]

    def _call(self, event: ToolCall) -> list[sc.SidecarEvent]:
        item_id = event.tool_id or self._mint()
        if item_id in self._items:
            return []
        item = _Item(
            kind="tool",
            status=orch.ITEM_STATUS_RUNNING,
            tool=event.tool,
            call_id=event.tool_id or "",
            args_json=_args_json(event.args),
        )
        self._items[item_id] = item
        return [self._event(item=item.proto(item_id, self._turn_id))]

    def _result(self, event: ToolResult) -> list[sc.SidecarEvent]:
        item_id = event.tool_id or self._mint()
        item = self._items.get(item_id)
        if item is None:
            item = self._items[item_id] = _Item(
                kind="tool",
                status=orch.ITEM_STATUS_RUNNING,
                tool=event.tool,
                call_id=event.tool_id or "",
            )
        output = event.stdout + (
            ("\n" if event.stdout else "") + event.stderr if event.stderr else ""
        )
        finished = _Item(
            kind="tool",
            status=orch.ITEM_STATUS_FINISHED,
            tool=item.tool or event.tool,
            call_id=item.call_id,
            args_json=item.args_json,
            output=output,
            exit_code=event.exit_code,
            is_error=bool(event.exit_code),
        )
        if item == finished:
            return []
        self._items[item_id] = finished
        return [self._event(item=finished.proto(item_id, self._turn_id))]

    def _delta(self, event: TextDelta) -> list[sc.SidecarEvent]:
        if event.item_id is None or not event.text:
            return []
        delta = sc.SidecarDelta(item_id=event.item_id)
        if event.kind is TextKind.TOOL_OUTPUT:
            delta.command_output = event.text
        else:
            delta.text = event.text
        return [self._event(delta=delta)]

    # -- usage and steers

    def _report(self, usage: ProviderUsage) -> list[sc.SidecarEvent]:
        proto = usage_proto(usage)
        if self._usage is not None and self._usage == proto:
            return []
        self._usage = proto
        return [self._event(usage=sc.UsageReported(turn_id=self._turn_id, usage=proto))]

    def _steer(self, text: str, *, accepted: bool) -> list[sc.SidecarEvent]:
        message_id = self._steers.take(text)
        return [] if message_id is None else [self._outcome(message_id, accepted=accepted)]

    @staticmethod
    def _outcome(message_id: str, *, accepted: bool) -> sc.SidecarEvent:
        return TurnNormalizer._event(
            steer=sc.SteerOutcome(message_id=message_id, accepted=accepted)
        )

    @staticmethod
    def _event(**body: object) -> sc.SidecarEvent:
        return sc.SidecarEvent(**body)  # type: ignore[arg-type]


def _args_json(args: object) -> str:
    if args is None:
        return ""
    if isinstance(args, str):
        return args
    return json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)


def normalize(
    turn_id: str,
    events: Iterable[AgentEvent],
    status: int,
    usage: ProviderUsage | None = None,
) -> list[sc.SidecarEvent]:
    """The sidecar events one whole turn gives, for tests and replay."""
    normalizer = TurnNormalizer(turn_id)
    out: list[sc.SidecarEvent] = []
    for event in events:
        out.extend(normalizer.feed(event))
    out.extend(normalizer.finish(status, usage))
    return out
