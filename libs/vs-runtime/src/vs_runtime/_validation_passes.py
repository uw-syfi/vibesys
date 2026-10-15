"""Trusted local-validation passes, kept by the runtime outside every agent workspace.

A recipe whose exact inputs already passed need not run again. Whether they passed is a
trust decision, so it is answered only by what the runtime itself recorded after running
the recipe: a ``ValidationPasses`` store. Nothing in an agent's workspace is consulted, so
a file the agent wrote cannot stand in for a run.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Protocol

from vs_runtime._local_validation import FrameworkValidationResult

if TYPE_CHECKING:
    from vs_project.api import StateNamespace
    from vs_runtime._local_validation import ValidationRecipe


class ValidationPasses(Protocol):
    """Recorded passes of trusted validation recipes, keyed by recipe and input digest."""

    def find(self, recipe: ValidationRecipe, input_digest: str) -> FrameworkValidationResult | None:
        """The recorded pass of exactly this recipe over exactly these inputs, if any."""
        ...

    def record(self, result: FrameworkValidationResult) -> None:
        """Remember a pass the runtime observed; raises ``ValueError`` for anything else."""
        ...


def _key(recipe: ValidationRecipe, input_digest: str) -> str:
    identity = json.dumps(
        {"recipe": recipe.model_dump(mode="json"), "input_digest": input_digest},
        sort_keys=True,
    )
    return hashlib.sha256(identity.encode()).hexdigest()


def _require_pass(result: FrameworkValidationResult) -> FrameworkValidationResult:
    if not result.passed or result.reused or not result.input_digest:
        message = "only a pass the runtime ran itself, over hashed inputs, is recorded"
        raise ValueError(message)
    return result


class StateValidationPasses:
    """Passes kept as typed state documents in a run's machine-local namespace."""

    def __init__(self, namespace: StateNamespace) -> None:
        """Keep passes below ``namespace``, which lies outside every workspace."""
        self._namespace = namespace

    def find(self, recipe: ValidationRecipe, input_digest: str) -> FrameworkValidationResult | None:
        """The stored pass for this recipe and digest, checked against both."""
        stored = self._namespace.load_optional(
            f"passes/{_key(recipe, input_digest)}.json", FrameworkValidationResult
        )
        if stored is None or stored.recipe != recipe or stored.input_digest != input_digest:
            return None
        return stored

    def record(self, result: FrameworkValidationResult) -> None:
        """Durably store one observed pass."""
        _require_pass(result)
        self._namespace.save(f"passes/{_key(result.recipe, result.input_digest)}.json", result)


class MemoryValidationPasses:
    """Passes kept in memory, for one process; the Fake of ``StateValidationPasses``."""

    def __init__(self) -> None:
        """Start with no passes."""
        self._passes: dict[str, FrameworkValidationResult] = {}

    def find(self, recipe: ValidationRecipe, input_digest: str) -> FrameworkValidationResult | None:
        """The remembered pass for this recipe and digest."""
        return self._passes.get(_key(recipe, input_digest))

    def record(self, result: FrameworkValidationResult) -> None:
        """Remember one observed pass."""
        self._passes[_key(result.recipe, result.input_digest)] = _require_pass(result)


__all__ = ["MemoryValidationPasses", "StateValidationPasses", "ValidationPasses"]
