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


def start_sandbox(sandbox: Sandbox) -> None:
    """Start a sandbox that owns an execution environment.

    The base :class:`Sandbox` contract intentionally has no lifecycle because
    host sandboxes have nothing to start. Callers that requested a container
    sandbox use this adapter and receive an immediate, named failure if the
    backend returned a sandbox without the required capability.
    """
    start = getattr(sandbox, "start", None)
    if not callable(start):
        message = f"{type(sandbox).__name__} has no execution environment to start"
        raise TypeError(message)
    start()


def stop_sandbox(sandbox: Sandbox) -> None:
    """Stop a sandbox-owned execution environment when one exists."""
    stop = getattr(sandbox, "stop", None)
    if callable(stop):
        stop()


class SandboxSession[ViewT]:
    """Context-managed ownership of one sandbox and its caller-defined view.

    ``borrowed`` wraps a sandbox whose lifecycle is owned elsewhere. ``start``
    starts a lifecycle-capable sandbox and makes this session responsible for
    stopping it. Cleanup is idempotent in both cases.

    The view is opaque to this library. It lets an application pair its own
    immutable description with the owned sandbox without moving application
    policy into ``vs_sandbox``.
    """

    def __init__(
        self,
        sandbox: Sandbox,
        view: ViewT,
        *,
        stop_on_close: bool,
    ) -> None:
        """Record ownership selected by one of the named constructors."""
        self.sandbox = sandbox
        self.view = view
        self._stop_on_close = stop_on_close
        self._closed = False

    @classmethod
    def borrowed(cls, sandbox: Sandbox, view: ViewT) -> SandboxSession[ViewT]:
        """Pair a view with a sandbox whose lifecycle the caller still owns."""
        return cls(sandbox, view, stop_on_close=False)

    @classmethod
    def start(cls, sandbox: Sandbox, view: ViewT) -> SandboxSession[ViewT]:
        """Start a sandbox and own its cleanup for the session lifetime."""
        start_sandbox(sandbox)
        return cls(sandbox, view, stop_on_close=True)

    def __enter__(self) -> SandboxSession[ViewT]:
        """Return this opened session."""
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        """Release the sandbox on every context-manager exit path."""
        self.close()

    def close(self) -> None:
        """Stop an owned sandbox at most once."""
        if self._closed:
            return
        self._closed = True
        if self._stop_on_close:
            stop_sandbox(self.sandbox)


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
