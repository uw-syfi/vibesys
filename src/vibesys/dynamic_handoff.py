"""The owner of the dynamic search's handoff files: one Markdown file per planning call.

The search asks for a `WriteHandoff` when a planning call schedules a plan. This owner renders
the request's data with the dynamic templates (`handoff/call.j2`) and writes the file into the
run's agent-visible state, opened through `Project`, so it is kept with the run and survives a
restart or a resume. It answers with the file's path relative to the project, which is where
the planner's workspace (the run's project checkout) sees it. The run's own record
(`.vibesys/state/runs/`) is hidden from agent confinement, so these files live beside it,
in the one state directory the confinement leaves readable and read-only.

Runs that began before the files moved kept them in the run's record. The owner copies
those into the agent-visible state the first time it is used, so a resumed run lists them
where its planner can read them; the earlier copies stay, unread, in the hidden record.

The planner itself never writes: its role reads only (see docs/contributing/sandboxing.md), and
its reply is the one source of what the file says. Writing the same request again writes the
same bytes, so a replay is safe, and `inspect` says the write landed when the file holds them.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Protocol, cast

from vibesys.orchestration.dynamic.core_policy.api import prompts
from vibesys.orchestration.dynamic.search.api import HANDOFF, HandoffWritten, WriteHandoff
from vs_prompts.api import TemplateRenderer
from vs_runtime.api.core import Applied, Inspection, KeyedWrite, NotApplied, OperationEntry

if TYPE_CHECKING:
    from pydantic import BaseModel

    from vs_core.api import OperationRequest
    from vs_runtime.api import OwnedOperationResources
    from vs_runtime.api.core import ExecutionContext

HANDOFF_NAMESPACE = "orchestrator-handoff"
"""The agent-visible state namespace that holds the handoff files."""

_RENDERER = TemplateRenderer(Path(prompts.__file__).parent)


class HandoffDocuments(Protocol):
    """Where the handoff files are kept: named documents in one run-state namespace."""

    def read_bytes(self, relative_path: str | PurePosixPath) -> bytes | None:
        """One document's bytes, or None when it is absent."""
        ...

    def write_bytes(self, relative_path: str | PurePosixPath, contents: bytes) -> None:
        """Replace one document atomically."""
        ...

    def agent_visible_path(self, relative_path: str | PurePosixPath | None = None) -> str:
        """The document's path relative to the project, as an agent's prompt names it."""
        ...

    def entries(self, relative_directory: str | PurePosixPath) -> tuple[str, ...]:
        """The names of the documents in one directory of the namespace, sorted."""
        ...


_FILENAME = re.compile(r"call-[0-9]{4,}\.md")


_DIRECTORY = "calls"
"""The namespace's directory of handoff files, which the owner lists."""


def handoff_filename(call: int) -> str:
    """The file of the ``call``-th planning call, within the namespace."""
    return f"{_DIRECTORY}/call-{call:04d}.md"


def _call_of(name: str) -> int:
    return int(name.removeprefix("call-").removesuffix(".md"))


def render_handoff(request: WriteHandoff) -> str:
    """The text of one handoff file, from the request's data only."""
    return _RENDERER.render_template(
        "handoff/call.j2",
        call=request.call,
        reasoning=request.reasoning,
        scheduled=request.scheduled,
        updates=request.updates,
        handoff=request.handoff,
        merges=request.merges,
    )


class HandoffOwner:
    """Perform and inspect the handoff write over one run's documents."""

    def __init__(
        self, documents: HandoffDocuments, *, earlier: HandoffDocuments | None = None
    ) -> None:
        """Bind the namespace the files are written to, and where an older run kept them."""
        self._documents = documents
        self._earlier = earlier

    async def execute(self, request: OperationRequest, context: ExecutionContext) -> BaseModel:
        """Write the file; a failed write propagates as an unknown effect."""
        del context
        return self.perform(cast("WriteHandoff", request))

    async def inspect(self, request: OperationRequest, context: ExecutionContext) -> Inspection:
        """Applied when the file already holds exactly what the request renders."""
        del context
        return self.observe(cast("WriteHandoff", request))

    def observe(self, request: WriteHandoff) -> Inspection:
        """Whether the file holds what ``request`` renders now: the body of `inspect`."""
        self._adopt_earlier_files()
        name = handoff_filename(request.call)
        if self._documents.read_bytes(name) == render_handoff(request).encode("utf-8"):
            return Applied(self._written(name))
        return NotApplied()

    def perform(self, request: WriteHandoff) -> HandoffWritten:
        """Write one handoff file now and say where it is."""
        self._adopt_earlier_files()
        name = handoff_filename(request.call)
        self._documents.write_bytes(name, render_handoff(request).encode("utf-8"))
        return self._written(name)

    def _adopt_earlier_files(self) -> None:
        """Copy the files an older run kept in its record to where the planner reads them."""
        if self._earlier is None:
            return
        held = set(self._documents.entries(_DIRECTORY))
        for item in self._earlier.entries(_DIRECTORY):
            if not _FILENAME.fullmatch(item) or item in held:
                continue
            contents = self._earlier.read_bytes(f"{_DIRECTORY}/{item}")
            if contents is not None:
                self._documents.write_bytes(f"{_DIRECTORY}/{item}", contents)

    def _written(self, name: str) -> HandoffWritten:
        """The file just written, with every handoff file the namespace holds, oldest first."""
        held = sorted(
            (item for item in self._documents.entries(_DIRECTORY) if _FILENAME.fullmatch(item)),
            key=_call_of,
        )
        return HandoffWritten(
            path=self._documents.agent_visible_path(name),
            listed=tuple(
                self._documents.agent_visible_path(f"{_DIRECTORY}/{item}") for item in held
            ),
        )


def handoff_entries(resources: OwnedOperationResources) -> tuple[OperationEntry, ...]:
    """The handoff write, owned by an owner over the run's handoff namespace."""
    state = resources.project.state
    namespace = state.agent_visible_namespace(resources.run_id, HANDOFF_NAMESPACE)
    earlier = state.portable_namespace(resources.run_id, HANDOFF_NAMESPACE)
    owner = HandoffOwner(namespace, earlier=earlier)
    return (
        OperationEntry.owned(HANDOFF, owner, KeyedWrite("the handoff file of the planner call")),
    )


__all__ = [
    "HANDOFF_NAMESPACE",
    "HandoffDocuments",
    "HandoffOwner",
    "handoff_entries",
    "handoff_filename",
    "render_handoff",
]
