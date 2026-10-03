"""Product presentation for lower-owned run environments."""

from __future__ import annotations

import shlex

from vibesys.prompts import PROMPTS_DIR, render_template
from vs_runtime.api.infrastructure import (
    DockerEnvironmentFacts,
    LocalEnvironmentFacts,
    ModalEnvironmentFacts,
    RunEnvironment,
    RunEnvironmentPresentation,
    RunEnvironmentRequest,
    RunEnvironmentSession,
    SkyPilotEnvironmentFacts,
    SlurmEnvironmentFacts,
)

_TEMPLATE_DIR = PROMPTS_DIR / "environments"


def open_run_environment(
    environment: RunEnvironment, request: RunEnvironmentRequest
) -> RunEnvironmentSession:
    """Render product policy and open a prepared infrastructure environment."""
    prepared = environment.prepare(request)
    facts = prepared.presentation_facts
    if isinstance(facts, LocalEnvironmentFacts):
        presentation = RunEnvironmentPresentation(prompt_notes="")
    elif isinstance(facts, DockerEnvironmentFacts):
        presentation = RunEnvironmentPresentation(
            prompt_notes=render_template(
                "docker/prompt_notes.j2",
                template_dir=_TEMPLATE_DIR,
                history_root=request.git_history_root,
            )
        )
    elif isinstance(facts, SlurmEnvironmentFacts):
        presentation = RunEnvironmentPresentation(
            prompt_notes=render_template(
                "slurm/prompt_notes.j2",
                template_dir=_TEMPLATE_DIR,
                service_command=shlex.join(facts.service_command),
                read_only_paths=[
                    path.as_posix() for path in request.project_path_policy.read_only_paths
                ],
            ).strip()
        )
    elif isinstance(facts, SkyPilotEnvironmentFacts):
        presentation = RunEnvironmentPresentation(
            prompt_notes=render_template(
                "skypilot/prompt_notes.j2",
                template_dir=_TEMPLATE_DIR,
                runtime_container_path=facts.runtime_container_path,
            ),
            runtime_document=render_template(
                "skypilot/runtime_notes.j2",
                template_dir=_TEMPLATE_DIR,
                nodes=facts.resources.nodes,
                accelerators_per_node=facts.resources.accelerators_per_node,
                accelerator_type=facts.resources.accelerator_type,
                profile_name=facts.resources.profile_name,
            ),
        )
    elif isinstance(facts, ModalEnvironmentFacts):
        presentation = RunEnvironmentPresentation(
            prompt_notes=render_template(
                "modal/prompt_notes.j2",
                template_dir=_TEMPLATE_DIR,
                runtime_container_path=facts.runtime_container_path,
            ),
            runtime_document=render_template(
                "modal/runtime_notes.j2",
                template_dir=_TEMPLATE_DIR,
                gpu=facts.gpu,
                app_name=facts.app_name,
                seeded_workspace_paths=request.seeded_workspace_paths,
                reference_path=facts.reference_path,
                history_root=request.git_history_root,
            ),
        )
    else:
        message = f"unsupported environment presentation facts: {type(facts).__name__}"
        raise TypeError(message)
    return prepared.open(presentation)
