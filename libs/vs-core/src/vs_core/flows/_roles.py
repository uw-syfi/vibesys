"""``Role``: the one declaration of everything about how an agent is asked.

A search declares each agent it asks once, next to the search::

    IMPLEMENTER = Role(
        name="implementer", title="Implementer", prompt="dynamic/implementer.j2", reply=ImplementerResult,
        workspace=WorkspaceMode.ISOLATED_CHILD, access=Access.WRITE_CANDIDATE,
        turn_time=minutes(45), corrections=1, lost_turns=2,
        may_wait_for_evaluations=True, charge=Charge.ATTEMPT, exchanges=EXCHANGES,
    )

and lists the roles in ``@search(..., roles=(...))``. Core holds no agent rule of its own: how
long a turn may take, how often an invalid reply is corrected, how often a lost turn is asked
again and after what wait, which failed replies may be corrected, how long a prompt may take to
render, whether the reply waits for evaluations, and whether the reply must wait for core's
checkpoint are all declared here. Core keeps only mechanism, which no role can lift: every wait
is bounded, and the caps below bound every field that repeats work.

Every field is required or has its default written here. A role is frozen and rejects unknown
keys; ``model`` is the one setting an operator may change per run (``SearchDef.with_models``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from vs_core._waitable import WaitableReply
from vs_core.types.agents import Backoff, Charge, Correctable, Exchanges
from vs_core.types.common import (
    MAX_CORRECTIONS,
    MAX_LOST_TURNS,
    SchemaRef,
    Seconds,
    Value,
    WorkspaceMode,
)
from vs_core.types.sessions import Access
from vs_core.types.until import ReplyError, ReplyErrorCode

if TYPE_CHECKING:
    from collections.abc import Mapping

    from pydantic import ValidationError

MAX_TURN_SECONDS = 6 * 3600.0
"""The longest any role may allow one provider turn."""
MAX_RENDER_SECONDS = 3600.0
"""The longest any role may allow a prompt to render."""


def minutes(count: float) -> Seconds:
    """``count`` minutes, in the seconds every duration is given in."""
    return count * 60.0


def hours(count: float) -> Seconds:
    """``count`` hours, in the seconds every duration is given in."""
    return count * 3600.0


MARKDOWN_PROSE_CONVENTION = (
    "Markdown: short paragraphs, `-` lists, `code` for identifiers and paths, fenced blocks "
    "for code or commands; no raw HTML, no headings above ###."
)
"""The one statement of how an agent writes a free-text handoff field of its reply."""
PLAIN_LINE_CONVENTION = "Plain text on one line, no Markdown."
"""The statement for a title-like field, which stays plain so a list can show it as is."""


def markdown_prose(description: str) -> str:
    """The description of a free-text reply field: what it holds, then the Markdown convention.

    The agent reads field descriptions as its reply schema, so building each prose field's
    description here keeps one statement of the convention for every reply model.
    """
    return f"{description} {MARKDOWN_PROSE_CONVENTION}"


def plain_line(description: str) -> str:
    """The description of a title-like reply field: what it holds, then that it is plain text."""
    return f"{description} {PLAIN_LINE_CONVENTION}"


class RevisionReply(Value):
    """Base of the reply of a role that works on a candidate and may leave work to keep.

    Core holds such a reply until it recorded (or declined) the checkpoint of the turn, and
    hands the checkpoint to the search with the reply. A reply for which ``produces_revision``
    is false (the agent says it did nothing) is delivered at once, with no checkpoint.
    """

    def produces_revision(self) -> bool:
        """Whether this reply claims work that core must checkpoint (override to say otherwise)."""
        return True


class Role(BaseModel):
    """One agent a search may ask: what it is asked, what it answers and how core carries it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]*$")]
    """The role's identity in ids, logs and ``agent.toml``."""
    title: Annotated[
        str, Field(pattern=r"^[A-Z][A-Za-z0-9]*(?: [a-z0-9][A-Za-z0-9]*)*$", max_length=40)
    ]
    """What a person calls the role on a screen: sentence case (``Perf evaluator``), never the id."""
    prompt: Annotated[str, Field(min_length=1)]
    """The template file the renderer reads for the role's own turns."""
    reply: type[BaseModel]
    """The model of the reply. Core wraps it for a role that may wait for evaluations."""
    workspace: WorkspaceMode | None
    """The mode of the role's workspace revision, or None for a role that works in the run."""
    access: Access
    """What the role's turns may write."""
    turn_time: Annotated[float, Field(gt=0, le=MAX_TURN_SECONDS, allow_inf_nan=False)]
    """Seconds one provider turn may take."""
    corrections: Annotated[int, Field(ge=0, le=MAX_CORRECTIONS)]
    """How often core asks the agent to correct a reply before it reports the failure."""
    lost_turns: Annotated[int, Field(ge=0, le=MAX_LOST_TURNS)]
    """How often core asks again after the transport lost the turn."""
    charge: Charge
    """What the role's first turn in a session costs."""
    may_wait_for_evaluations: bool = False
    """Whether the role may end a turn waiting for evaluations it submitted (and be resumed)."""
    exchanges: Exchanges = Exchanges()
    """The templates of the exchanges core starts: corrections and resumes."""
    correctable: frozenset[Correctable] = frozenset(Correctable)
    """Which failed replies core may ask the agent to correct."""
    lost_turn_backoff: Backoff = Backoff(first=5.0, cap=120.0)
    """The wait before a lost turn is asked again; it doubles with each further loss."""
    render_time: Annotated[float, Field(gt=0, le=MAX_RENDER_SECONDS, allow_inf_nan=False)] = 300.0
    """Seconds a prompt of this role may take to render."""
    model: Annotated[str, Field(min_length=1, max_length=256)] | None = None
    """The model the role's turns use; None leaves the run's default."""
    agent: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]*$")] | None = None
    """The agent this role puts another question to, when a role of the search declares that agent.

    The agent's id, its system prompt and its model (``agent.toml``) are that role's; sessions,
    decisions and replies stay this role's own. None for a role that is its own agent.
    """

    @property
    def identity(self) -> str:
        """The name of the agent that answers this role: ``agent``, else the role's own name."""
        return self.name if self.agent is None else self.agent

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        problems: list[str] = []
        if issubclass(self.reply, WaitableReply):
            problems.append("reply: name the role's own model; core wraps it when the role waits")
        if issubclass(self.reply, RevisionReply) and (
            self.workspace is None or self.access is not Access.WRITE_CANDIDATE
        ):
            problems.append("reply: a revision reply needs a workspace and write-candidate access")
        if (
            self.corrections
            and Correctable.INVALID_REPLY in self.correctable
            and self.exchanges.correction is None
        ):
            problems.append("exchanges.correction: needed to correct invalid replies")
        if (
            self.corrections
            and self.may_wait_for_evaluations
            and Correctable.UNRECORDED_WAIT in self.correctable
            and self.exchanges.wait_unrecorded is None
        ):
            problems.append("exchanges.wait_unrecorded: needed to correct an unrecorded wait")
        if self.may_wait_for_evaluations and self.exchanges.resume is None:
            problems.append("exchanges.resume: needed by a role that waits for evaluations")
        if problems:
            message = f"role {self.name!r}: " + "; ".join(problems)
            raise ValueError(message)
        return self

    @property
    def reply_schema(self) -> SchemaRef:
        """The schema the role's turns are asked to answer with: derived from its name."""
        return SchemaRef(name=f"agent.{self.name}.reply", version=1)

    @property
    def wire_reply(self) -> type[BaseModel]:
        """The model the agent answers with on the wire: the reply, or a result-or-wait of it."""
        if self.may_wait_for_evaluations:
            return WaitableReply[self.reply]  # ty: ignore[invalid-type-form]
        return self.reply

    def reply_needs_checkpoint(self, reply: BaseModel) -> bool:
        """Whether ``reply`` must be held until core knows the checkpoint of its turn."""
        return isinstance(reply, RevisionReply) and reply.produces_revision()


class UnknownRoleError(ValueError):
    """A per-run setting names a role the search does not declare."""

    def __init__(self, names: list[str], declared: list[str]) -> None:
        """Name the unknown roles and the declared ones."""
        super().__init__(
            f"unknown role(s) {', '.join(names)}; the search declares {', '.join(declared)}"
        )


def with_models(roles: tuple[Role, ...], models: Mapping[str, str]) -> tuple[Role, ...]:
    """The roles with the models an operator chose, by agent; an unknown agent is an error."""
    declared = {role.identity for role in roles}
    unknown = sorted(models.keys() - declared)
    if unknown:
        raise UnknownRoleError(unknown, sorted(declared))
    return tuple(
        role.model_copy(update={"model": models[role.identity]})
        if role.identity in models
        else role
        for role in roles
    )


def reply_errors(error: ValidationError) -> tuple[ReplyError, ...]:
    """The typed problems of a reply that broke its model."""
    found: list[ReplyError] = []
    for item in error.errors():
        kind = item["type"]
        if kind == "missing":
            code = ReplyErrorCode.MISSING
        elif kind == "extra_forbidden":
            code = ReplyErrorCode.UNEXPECTED
        elif kind == "json_invalid":
            code = ReplyErrorCode.NOT_JSON
        elif kind.endswith(("_type", "_parsing")):
            code = ReplyErrorCode.WRONG_TYPE
        else:
            code = ReplyErrorCode.INVALID_VALUE
        path = tuple(part for part in item["loc"] if isinstance(part, str | int))
        found.append(ReplyError(path=path, code=code))
    return tuple(found) or (ReplyError(path=(), code=ReplyErrorCode.INVALID_VALUE),)
