# the module-level skip below has to precede the imports it guards
from typing import TYPE_CHECKING

import pytest

# Guarded so type checkers still see the module body: the names below are
# imported by other test modules, and an unconditional skip makes a checker
# infer every one of them as unreachable.
if not TYPE_CHECKING:
    pytest.skip("superseded by the 0.6 driver rewrite", allow_module_level=True)

import inspect

import agentshim

import vibesys._agent_cli.cli_agent


def test_recorder_api_removed_in_favor_of_agent_event_handler():  # noqa: ANN202  # tracked: #288
    assert not hasattr(agentshim, "trajectory")
    assert (
        "recorder"
        not in inspect.signature(vibesys._agent_cli.cli_agent.CLICodingAgent.__init__).parameters  # noqa: SLF001  # tracked: #288
    )
    assert (
        "recorder"
        not in inspect.signature(
            vibesys._agent_cli.cli_agent.CLIGenerationSession.__init__  # noqa: SLF001  # tracked: #288
        ).parameters
    )
