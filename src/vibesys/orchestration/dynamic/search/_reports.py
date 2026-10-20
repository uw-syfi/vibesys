"""What workstreams tell the root while they run, and what the root answers each one.

A workstream reports on the `REPORTS` channel, in the order things happen to it: it submitted a
candidate for its trusted measurement (`Submission`), its measurement no longer holds a slot
(`Released`), it has waited for the input's measurement longer than the run allows
(`InputOverdue`), its workspace opened and it waits for its first agent slot (`Opened`), core
suspended one of its agent turns on the turn's own evaluations
(`Suspended`), core authorized that turn's resume (`Resuming`). The root folds each report into
its record and answers a submission on the workstream's own `VERDICTS` latch: measure now, or
never (`_queue.SlotVerdict`); and a resume on its `RESUMES` latch, once an agent slot is free.
"""

from vibesys.orchestration.dynamic.search._queue import SlotVerdict, Submission
from vs_core.api.search import ChannelKey, Flow, LatchKey, Value


class Released(Value):
    """The trusted measurement of the workstream ``key`` is over: its slot is free."""

    key: str


class InputOverdue(Value):
    """The workstream ``key`` has waited ``limit`` seconds for the input's measurement."""

    key: str
    since: float
    """When it began to wait, on the run clock."""
    limit: float


class Opened(Value):
    """The workspace of the workstream ``key`` is open (again): it waits for its first agent slot."""

    key: str


class Suspended(Value):
    """Core suspended the ``wait``-th agent turn of the workstream ``key`` on its evaluations."""

    key: str
    wait: int


class Resuming(Value):
    """Core authorized the resume of the ``wait``-th suspended turn of the workstream ``key``."""

    key: str
    wait: int


class Granted(Value):
    """The root lent a resuming turn an agent slot: its agent may go on."""


type Report = Submission | Released | InputOverdue | Opened | Suspended | Resuming

REPORTS = ChannelKey[Report]("workstream-reports")
"""Every workstream reports here; the root reads it."""

VERDICTS = LatchKey[SlotVerdict]("evaluation-verdict")
"""The root's answer to a submission, one latch per workstream key (``VERDICTS.of(key)``)."""

RESUMES = LatchKey[Granted]("agent-slot")
"""The root's grant of an agent slot to a workstream's resuming turn: ``RESUMES.of(f"{key}.{wait}")``
for its ``wait``-th suspended turn. Wait 0 is the workstream's first slot, lent once its workspace
opened (`seated`), or once it reopened after a code change (`RunRecord.held_over`)."""


async def seated(ctx: Flow, key: str) -> None:
    """Report that the workspace of ``key`` is open, and return once the root lent it a slot.

    Its attempt was admitted, so the slot it takes frees again whatever core's admission limit.
    """
    ctx.channel(REPORTS).send(Opened(key=key))
    await ctx.latch(RESUMES.of(f"{key}.0"))


class WorkstreamParking:
    """How the workstream ``key`` parks its agent slot while core suspends one of its turns.

    The `vs_core.api.search.Parking` of its turns: it reports the wait, so the root may lend the
    slot to other work, and goes on once the root lends it a slot again.
    """

    def __init__(self, ctx: Flow, key: str) -> None:
        """Park the turns of the workstream ``key``, numbering its waits from 1."""
        self._ctx, self._key, self._waits = ctx, key, 0

    def parked(self) -> None:
        """Report the turn's wait: its agent slot is free."""
        self._waits += 1
        self._ctx.channel(REPORTS).send(Suspended(key=self._key, wait=self._waits))

    async def resumed(self) -> None:
        """Report the authorized resume, and return once the root lent the turn a slot."""
        self._ctx.channel(REPORTS).send(Resuming(key=self._key, wait=self._waits))
        await self._ctx.latch(RESUMES.of(f"{self._key}.{self._waits}"))
