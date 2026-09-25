"""Reply-schema pieces shared across role families.

``Verdict`` and ``SkillResourceSelection`` appear in more than one family's
reply schema (implementer, judge, single-agent), so they live here instead of
being duplicated or owned by one family's module.

Deviation from the design brief: ``SkillResourceSelection``'s canonical
definition stays in ``vibesys.schemas`` (re-exported here), not defined here,
because ``vibesys.search.hypothesis.plan.OrchestratorPlan.recommended_skills``
also needs it, and ``search`` must never depend on ``vibesys.roles``. See
``schemas.py``'s module docstring.
"""

from __future__ import annotations

from enum import StrEnum

from vibesys.schemas import SkillResourceSelection

__all__ = ["SkillResourceSelection", "Verdict"]


class Verdict(StrEnum):  # noqa: D101  # tracked: #288
    PASS = "pass"  # noqa: S105  # tracked: #288
    FAIL = "fail"
