"""Plain constants shared across ``entrypoints.cli`` submodules.

Dependency-free (no imports from other ``cli`` submodules) so every submodule
can import from here without risking an import cycle.
"""

from __future__ import annotations

_OUTER_LOOPS = ("agent", "profile-guided", "dynamic", "plain", "evolve")


_IGNORED_CONFIG_SECTIONS = frozenset()


_RUN_ENVIRONMENT_OPTION_CLI_FIELDS: dict[str, str] = {
    "docker_image": "image",
    "modal_gpu": "gpu",
    "modal_model_volume": "model_volume",
    "modal_app": "app",
    "slurm_config": "config_path",
}


_STANDALONE_INPUT_DESTS = (
    "input_objective",
    "input_objective_file",
    "input_domain",
    "input_accuracy_command",
    "input_benchmark_command",
    "input_accuracy_timeout",
    "input_benchmark_timeout",
    "input_benchmark_metric",
    "input_benchmark_result_arg",
    "input_reference",
    "input_evaluator_dir",
    "input_evaluator_source",
)

CLI_PROVIDER_FLAG = "--cli-provider"
LOCAL_FLAG = "--local"
MAX_RUN_SECONDS_FLAG = "--max-run-seconds"
MODEL_FLAG = "--model"
PUBLISH_FLAG = "--publish"
REPO_FLAG = "--repo"
ROLE_MODEL_FLAG = "--role-model"
ROLE_PROVIDER_FLAG = "--role-provider"
ROLE_REASONING_EFFORT_FLAG = "--role-reasoning-effort"
UNVETTED_MODEL_FLAG = "--allow-unvetted-model"
