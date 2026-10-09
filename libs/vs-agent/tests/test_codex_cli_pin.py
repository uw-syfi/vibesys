"""The container's Codex CLI must speak the app-server protocol agentshim decodes."""

from __future__ import annotations

from agentshim.providers.codex.app_server import CODEX_VERSION

from vs_agent.api import CLI_VERSIONS


def _numbers(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def test_the_container_codex_cli_is_not_older_than_agentshims_protocol() -> None:
    # test-isolation: agentshim exports the protocol's CLI version only from its Codex
    # app-server package; there is no top-level name, and this pin is the contract.
    assert _numbers(CLI_VERSIONS["codex"]) >= _numbers(CODEX_VERSION)
