"""Identities of the issue-queue strategy: the shared core grammar under the `plain` namespace.

Every identity is a pure function of counters and names in the strategy state, so replaying
`decide` after a restart re-proposes the same canonical identities.
"""

from vs_core.api import IdScheme, attempt_id, attempt_ref, decision_of_operation, item_id

__all__ = [
    "IDS",
    "attempt_id",
    "attempt_ref",
    "decision_id",
    "decision_of_operation",
    "item_id",
    "role_id",
]

# The role prefix matches the role ids the host registers (`vibesys.issue_queue_core`).
IDS = IdScheme("plain", role_prefix="plain-")
decision_id = IDS.decision_id
role_id = IDS.role_id
