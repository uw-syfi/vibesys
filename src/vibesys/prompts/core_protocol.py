"""The core lifecycle's agent-facing protocol: typed contexts and the templates they render.

Every strategy on the core lifecycle speaks the same protocol to its agents when a reply violates
its schema, when a suspended turn resumes after its awaited evaluations, when a reply waits and
the run recorded no wait, and when it explains the in-turn evaluation tool and the run
environment. The wording lives in ``core_protocol/*.j2``; a strategy lists these directories as
the fallback roots of its core policy (`CORE_PROTOCOL_DIR`) and adds the contexts below to its
own prompt-context union.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Literal

from vs_core.api import EvidenceId, RevisionRef, Value

CORE_PROTOCOL_DIR = Path(__file__).resolve().parent / "core_protocol"
"""Directory of the shared protocol templates, a fallback root of a core policy."""


class CoreProtocolTemplate(StrEnum):
    """The shared templates a strategy can request through its render operation."""

    RESUME = "resume"
    REPLY_CORRECTION = "reply_correction"
    WAIT_UNRECORDED = "wait_unrecorded"


class ResumePrompt(Value):
    """Continuation of a suspended turn after its awaited evaluations settled."""

    template: Literal[CoreProtocolTemplate.RESUME] = CoreProtocolTemplate.RESUME
    role: Literal["implementer", "judge", "profiler"]
    retained_revision: RevisionRef | None
    timed_out: bool
    evidence: tuple[EvidenceId, ...] = ()
    repeated_failure: str | None = None


class ReplyCorrectionPrompt(Value):
    """Why the previous reply violated its output schema."""

    template: Literal[CoreProtocolTemplate.REPLY_CORRECTION] = CoreProtocolTemplate.REPLY_CORRECTION
    role: Literal["planner", "implementer", "judge", "profiler"]
    error: str


class WaitUnrecordedPrompt(Value):
    """The previous reply said it was waiting for an evaluation, but the run recorded no wait."""

    template: Literal[CoreProtocolTemplate.WAIT_UNRECORDED] = CoreProtocolTemplate.WAIT_UNRECORDED
