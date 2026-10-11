"""Single's identities: the shared core grammar under the `sgl` namespace.

Every identity is a pure function of counters and names in the strategy state, so replaying
`decide` after a restart re-proposes the same canonical identities. The role prefix matches the
role ids in `vibesys.single_roles`.
"""

from __future__ import annotations

from vs_core.api import (
    IdScheme,
    attempt_id,
    attempt_ref,
    component,
    decision_of_operation,
    item_id,
)

__all__ = [
    "IDS",
    "attempt_id",
    "attempt_ref",
    "component",
    "decision_id",
    "decision_of_operation",
    "invocation_id",
    "item_id",
    "role_id",
    "session_id",
]

IDS = IdScheme("sgl", role_prefix="single-")
decision_id = IDS.decision_id
invocation_id = IDS.invocation_id
session_id = IDS.session_id
role_id = IDS.role_id
