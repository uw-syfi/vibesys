"""Prompt-test inputs for a run whose domain pack addresses no role."""

from __future__ import annotations

from vibesys.domains.base import DOMAIN_ROLES

EMPTY_DOMAIN_SECTIONS: dict[str, str] = {role.value: "" for role in DOMAIN_ROLES}
