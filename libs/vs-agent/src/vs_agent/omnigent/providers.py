"""Which Omnigent executor backs each VibeSys CLI provider.

This module is the *application configuration* half of the Omnigent backend:
it names the executor class for every supported provider and owns nothing
else. It deliberately does not import ``omnigent`` — the mapping is data, so
it stays importable and unit-testable without the optional dependency
installed.

:mod:`vs_agent.drivers.omnigent` resolves these entries at session setup.

Coverage against ``omnigent==0.10.0``, verified by inspecting the installed
package rather than its docs:

- ``claude`` -> ``ClaudeSDKExecutor`` (harness ``claude-sdk``)
- ``codex``  -> ``CodexExecutor`` (harness ``codex``)
- ``gemini`` -> unsupported; Omnigent ships no Gemini harness at all.
- ``opencode`` -> unsupported here. ``opencode-native`` exists, but its
  ``OpenCodeNativeExecutor`` is a bridge for Omnigent's own web UI: it takes
  only ``bridge_dir`` — no ``cwd``, ``model``, or ``os_env`` — so it cannot
  run a headless turn in a VibeSys workspace.

Both supported entries accept ``cwd``/``model``/``os_env``, which is the
constructor shape :class:`~vs_agent.drivers.omnigent.OmnigentDriver`
depends on.
"""

from __future__ import annotations

from collections.abc import Mapping  # noqa: TC003  # tracked: #288
from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class OmnigentExecutorSpec:
    """Locates the Omnigent executor class for one VibeSys CLI provider.

    Attributes:
        harness: Omnigent's canonical harness identifier. Diagnostic only —
            VibeSys constructs the executor class directly rather than going
            through Omnigent's harness registry, so this exists to make log
            lines and error messages traceable back to Omnigent's own naming.
        module: Import path of the module holding the executor class.
        class_name: Executor class name within ``module``.
    """

    harness: str
    module: str
    class_name: str


OMNIGENT_PROVIDER_EXECUTORS: Mapping[str, OmnigentExecutorSpec] = MappingProxyType(
    {
        "claude": OmnigentExecutorSpec(
            harness="claude-sdk",
            module="omnigent.inner.claude_sdk_executor",
            class_name="ClaudeSDKExecutor",
        ),
        "codex": OmnigentExecutorSpec(
            harness="codex",
            module="omnigent.inner.codex_executor",
            class_name="CodexExecutor",
        ),
    }
)
"""VibeSys cli provider name -> Omnigent executor location."""


def supported_providers() -> list[str]:
    """Return the sorted provider names the Omnigent backend can run."""
    return sorted(OMNIGENT_PROVIDER_EXECUTORS)
