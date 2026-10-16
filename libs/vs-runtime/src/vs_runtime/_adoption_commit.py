"""The commit message that records an adopted revision on the run's branch.

Adoption makes the root workspace hold the winner. The run then commits it on its own
branch, so the project is clean when the run ends and the next run can start; the
user's home branch is never touched. The message names what was adopted and what it
measured, read from the measurements core embedded in the request. A measurement the
evidence ledger never recorded is left out, never guessed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vs_core.api import RetainedCandidate

if TYPE_CHECKING:
    from vs_core.api import AdoptRevision
    from vs_runtime._evidence_ledger import EvidenceLookup

_SHORT_COMMIT = 12


def adoption_commit_message(request: AdoptRevision, commit: str, lookup: EvidenceLookup) -> str:
    """Subject naming the adopted revision, then one body line per recorded metric."""
    short = commit[:_SHORT_COMMIT]
    selection = request.selection
    if isinstance(selection, RetainedCandidate):
        subject = f"vibesys: adopt candidate {selection.settlement_id.root} ({short})"
    else:
        subject = f"vibesys: keep the input revision ({short})"
    lines = [subject, "", f"Revision: {commit}"]
    for ref in request.evidence:
        entry = lookup.lookup(ref.key)
        if entry is None or entry.purpose != ref.purpose:
            continue
        lines.extend(
            f"Measured ({ref.purpose} {entry.evidence.stage_name}): "
            f"{metric.name} = {metric.value:.6g}"
            f"{'' if metric.unit is None else ' ' + metric.unit}"
            f"{_better(metric.direction)}"
            for metric in entry.evidence.metrics
        )
    return "\n".join(lines) + "\n"


def _better(direction: str | None) -> str:
    if direction == "min":
        return " (lower is better)"
    if direction == "max":
        return " (higher is better)"
    return ""
