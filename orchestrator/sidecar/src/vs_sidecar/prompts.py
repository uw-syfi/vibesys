"""The words an agent is sent for a message: rendered from templates, never concatenated.

``render`` takes the ``Message`` the orchestrator addresses to the agent and returns the prompt.
Each body kind has one template in ``prompts/``; Python passes the data and nothing else.
"""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from vs_sidecar.gen.vibesys.orchestrator.v1 import orchestrator_pb2 as orch

_ENV = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "prompts"),
    undefined=StrictUndefined,
    keep_trailing_newline=False,
    autoescape=select_autoescape(enabled_extensions=(), default=False),
    trim_blocks=True,
    lstrip_blocks=True,
)


def render(message: orch.Message) -> str:
    """The prompt for *message*. Raises ``ValueError`` for a message with no body."""
    kind = message.WhichOneof("body")
    context: dict[str, object] = {"message": message}
    if kind == "text":
        template = "text"
    elif kind == "brief":
        template = "brief"
    elif kind == "lead_brief":
        template, context["lead"] = "lead_brief", message.lead_brief
    elif kind == "handoff":
        template, context["handoff"] = "handoff", message.handoff
    elif kind == "report":
        template, context["report"] = "report", message.report
    elif kind == "job_finished":
        template, context["job"] = "job_finished", message.job_finished
        context["outcome"] = (
            orch.JobOutcome.Name(message.job_finished.outcome).removeprefix("JOB_OUTCOME_").lower()
        )
    elif kind == "subagent_settled":
        template, context["settled"] = "subagent_settled", message.subagent_settled
        context["status"] = (
            orch.AgentStatus.Name(message.subagent_settled.status)
            .removeprefix("AGENT_STATUS_")
            .lower()
        )
    else:
        msg = "a message with no body has no prompt"
        raise ValueError(msg)
    return _ENV.get_template(f"{template}.md.j2").render(context).strip()
