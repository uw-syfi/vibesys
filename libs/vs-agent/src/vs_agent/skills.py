"""Caller-supplied skill-materialization policy shared by core and the agent package."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable


@dataclass(frozen=True)
class SkillSelection:
    """Caller-supplied policy for which skill directories to skip while copying.

    ``skip_dir`` mirrors :func:`shutil.copytree`'s ``ignore`` callable
    signature: given the directory being copied and the names in it, return the
    subset to skip. Core injects the prune policy as data so the agent package
    need not resolve it from domain knowledge (compute backends) it should not
    have.
    """

    skip_dir: Callable[[str, list[str]], set[str]]


#: The no-op selection: skip nothing beyond whatever the mechanism already
#: excludes. The default so call sites need not build a lambda themselves.
NULL_SKILL_SELECTION = SkillSelection(skip_dir=lambda _src_dir, _names: set())
