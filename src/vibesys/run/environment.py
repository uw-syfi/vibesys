"""Product presentation for lower-owned run environments."""

from __future__ import annotations

import shlex

from vibesys.prompts import PROMPTS_DIR, render_template
from vs_project.api import Project
from vs_runtime.api.infrastructure import (
    DockerEnvironmentFacts,
    HostEnvironmentFacts,
    ModalEnvironmentFacts,
    RunEnvironment,
    RunEnvironmentPresentation,
    RunEnvironmentRequest,
    RunEnvironmentSession,
    RunEnvironmentView,
    SkyPilotEnvironmentFacts,
    SlurmEnvironmentFacts,
)

_TEMPLATE_DIR = PROMPTS_DIR / "environments"


def _placeholder_missing_hidden_files(request: RunEnvironmentRequest) -> None:
    """Create empty files for hidden paths a linked worktree did not check out.

    State directories are created by ``prepare_hidden_directories``. ``agent.toml``
    and root ``.env*`` stay in the canonical policy when they exist on the operator
    checkout, and a worktree of a commit does not contain those ignored files.
    Confinement masks a hidden path only when it exists. The placeholder is empty;
    the operator file is not copied.
    """
    workspace = request.workspace
    for relative in request.project_path_policy.hidden_paths:
        target = workspace / relative
        if target.exists() or target.is_symlink():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.touch()


def agent_environment_notes(view: RunEnvironmentView) -> str:
    """The run environment notes every agent prompt states, for the run's root session *view*.

    Prompts are rendered for the run, so the Git sentence follows where agents write
    (``RunEnvironmentView.agent_git_writable``), not the root session's own checkout.
    """
    return render_template(
        "agent_notes.j2",
        template_dir=_TEMPLATE_DIR,
        environment_notes=view.prompt_notes,
        git_writable=view.agent_git_writable,
    )


def open_run_environment(
    environment: RunEnvironment, request: RunEnvironmentRequest
) -> RunEnvironmentSession:
    """Render product policy and open a prepared infrastructure environment."""
    # Linked candidates omit ignored local state and any run record their revision
    # predates, but share the canonical confinement policy. Prepare its hidden
    # directories, and empty placeholders for ignored secret files the worktree
    # did not check out, before any sandbox opens.
    Project.open(request.workspace).state.prepare_hidden_directories(request.run_id)
    _placeholder_missing_hidden_files(request)
    prepared = environment.prepare(request)
    facts = prepared.presentation_facts
    if isinstance(facts, HostEnvironmentFacts):
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
                gpu_client=facts.gpu_client,
                agent_gpu=facts.agent_gpu,
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
