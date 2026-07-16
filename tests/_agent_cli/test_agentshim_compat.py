import inspect

import agentshim

import vibesys._agent_cli.cli_agent


def test_recorder_api_removed_in_favor_of_agent_event_handler():
    assert not hasattr(agentshim, "trajectory")
    assert (
        "recorder"
        not in inspect.signature(vibesys._agent_cli.cli_agent.CLICodingAgent.__init__).parameters
    )
    assert (
        "recorder"
        not in inspect.signature(
            vibesys._agent_cli.cli_agent.CLIGenerationSession.__init__
        ).parameters
    )
