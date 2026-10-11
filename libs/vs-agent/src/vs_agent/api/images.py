"""Public Docker image construction and publication API.

The implementation owns the agent image layout and applies VibeSys's fixed
provider and toolchain policy. Callers supply task-specific inputs and an
optional runner seam; they do not inspect the packaged Feature.
"""

from vs_agent.agent_feature import AGENT_FEATURES_PROJECT, AGENT_USER, AGENT_WORKDIR, agent_feature
from vs_agent.images import (
    DEFAULT_AGENT_IMAGE_REGISTRY,
    DEFAULT_IMAGE_BUILD_TIMEOUT_SECONDS,
    DOCKER_OUTPUT_TAIL_CHARACTERS,
    DOCKER_PROGRESS_LINE_CHARACTERS,
    BuildProgressSink,
    DockerBuildResult,
    DockerBuildRunner,
    DockerCommandResult,
    ImageBuildProgress,
    ImageBuildTimeoutError,
    ImagePushError,
    LegacyDockerCommandResult,
    SubprocessDockerBuildRunner,
    TaskImageBuildError,
    agent_image,
    agent_image_is_pushed,
    build_task_image,
    ensure_pushed,
    push_agent_image,
)

__all__ = [
    "AGENT_FEATURES_PROJECT",
    "AGENT_USER",
    "AGENT_WORKDIR",
    "DEFAULT_AGENT_IMAGE_REGISTRY",
    "DEFAULT_IMAGE_BUILD_TIMEOUT_SECONDS",
    "DOCKER_OUTPUT_TAIL_CHARACTERS",
    "DOCKER_PROGRESS_LINE_CHARACTERS",
    "BuildProgressSink",
    "DockerBuildResult",
    "DockerBuildRunner",
    "DockerCommandResult",
    "ImageBuildProgress",
    "ImageBuildTimeoutError",
    "ImagePushError",
    "LegacyDockerCommandResult",
    "SubprocessDockerBuildRunner",
    "TaskImageBuildError",
    "agent_feature",
    "agent_image",
    "agent_image_is_pushed",
    "build_task_image",
    "ensure_pushed",
    "push_agent_image",
]
