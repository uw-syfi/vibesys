"""Trusted lifecycle extensions for sandbox startup.

Lifecycle hooks are registered by framework code, not by candidate code.
The sandbox invokes :meth:`SandboxLifecycleHooks.before_ready` after its
execution environment accepts commands and before it is exposed to callers.
Hooks run again whenever a backend creates a replacement execution
environment, so implementations must be idempotent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from vs_sandbox.execution import Sandbox


@dataclass(frozen=True)
class BeforeReadyContext:
    """Resources available while a sandbox is transitioning to ready."""

    sandbox: Sandbox


class SandboxLifecycleHooks:
    """Base class for trusted sandbox lifecycle hooks.

    Future lifecycle points can be added here as concrete no-op methods. That
    keeps existing subclasses compatible while allowing one hooks provider
    to participate in more than one phase.
    """

    def before_ready(self, context: BeforeReadyContext) -> None:
        """Prepare an execution-capable sandbox before callers can use it."""


class SandboxLifecycleError(RuntimeError):
    """Raised when a lifecycle hook prevents a sandbox becoming ready."""

    def __init__(self, hook: str, provider: str, cause: Exception) -> None:
        """Name the failed hook and provider while retaining the cause."""
        super().__init__(f"{hook} hook in {provider} failed: {cause}")


class SandboxLifecycle:
    """Run an ordered, immutable snapshot of lifecycle hooks."""

    def __init__(
        self,
        hooks: Sequence[SandboxLifecycleHooks] | None = None,
    ) -> None:
        """Snapshot hooks providers in their deterministic execution order."""
        self._hooks = tuple(hooks or ())

    @property
    def hooks(self) -> tuple[SandboxLifecycleHooks, ...]:
        """Return the hooks providers in their deterministic execution order."""
        return self._hooks

    def before_ready(self, sandbox: Sandbox) -> None:
        """Run every provider's hook, stopping at the first failure."""
        context = BeforeReadyContext(sandbox=sandbox)
        for provider in self._hooks:
            try:
                provider.before_ready(context)
            except Exception as exc:
                name = type(provider).__name__
                raise SandboxLifecycleError("before_ready", name, exc) from exc
