"""The publication commit's description, derived from the selected revision's evidence.

Publishing creates the commit before the independent optional checkout update. No file or
index in the operator's checkout contributes bytes to the publication.
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
