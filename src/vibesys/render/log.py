"""Run-log helpers: write agent output to a log file and emit it on the output sink."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, TextIO

from vibesys.render.sink import output_sink

if TYPE_CHECKING:
    from vibesys.events import AgentOutputChannel


def log_agent_config(agent: Any, label: str, log_file: TextIO | None) -> None:  # noqa: ANN401  # tracked: #288
    """Write agent configuration (tools list) to log file."""
    if not log_file:
        return
    log_file.write(f"\n{'=' * 60}\n")
    log_file.write(f"  Agent Configuration: {label}\n")
    log_file.write(f"{'=' * 60}\n")

    # Extract tools
    try:
        tools_node = agent.builder.nodes["tools"]
        tools_dict = tools_node.runnable.tools_by_name
        log_file.write(f"\n  Tools ({len(tools_dict)}):\n")
        for name, tool in sorted(tools_dict.items()):
            desc = getattr(tool, "description", "")
            # Truncate long descriptions to first line
            first_line = desc.split("\n")[0] if desc else ""
            log_file.write(f"    - {name}: {first_line}\n")
    except (KeyError, AttributeError):
        log_file.write("  Tools: <unable to extract>\n")

    log_file.write("\n")
    log_file.flush()


def log_and_print(
    text: str,
    log_file: TextIO | None = None,
) -> None:
    """Emit *text* as a diagnostic event and write it in full to log_file.

    Display (including any truncation) is the subscribed renderer's job;
    the log always receives the untruncated text.
    """
    log_markdown_and_print(text, log_file=log_file, channel="diagnostic")


def log_markdown_and_print(
    text: str,
    log_file: TextIO | None = None,
    *,
    channel: AgentOutputChannel = "assistant",
) -> None:
    """Emit raw Markdown; presentation clients decide how to render it."""
    output_sink().agent_output(text + "\n", channel=channel)
    if log_file:
        log_file.write(text + "\n")
        log_file.flush()


def log_prompt_markdown_and_print(
    prompt: str,
    log_file: TextIO | None = None,
) -> None:
    """Emit raw prompt Markdown while preserving it in logs."""
    log_markdown_and_print(
        prompt,
        log_file=log_file,
        channel="prompt",
    )


def log_json_and_print(
    text: str,
    log_file: TextIO | None = None,
) -> None:
    """Emit raw JSON; presentation clients decide how to render it."""
    log_and_print(text, log_file=log_file)
