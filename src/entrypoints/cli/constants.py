"""Plain constants shared across ``entrypoints.cli`` submodules.

Dependency-free (no imports from other ``cli`` submodules) so every submodule
can import from here without risking an import cycle.
"""

from __future__ import annotations

_OUTER_LOOPS = ("agent", "profile-guided", "plain", "evolve")


_MODALITIES = (
    "text_generation",
    "image_generation",
    "video_generation",
    "text_to_speech",
    "speech_to_text",
    "realtime_audio",
    "kv_store",
)


_DEFAULT_CONFIG_TEXT = '[model]\nname = "gpt-5.4"\n'


_IGNORED_CONFIG_SECTIONS = frozenset({"tui"})


_COMMON_RESUME_CLI_FIELDS: dict[str, str] = {
    "agent_backend": "agent_backend",
    "cli_provider": "cli_provider",
    "backend": "compute_backend",
    "profiler": "profiler",
    "modality": "modality",
}


_RUN_ENVIRONMENT_OPTION_CLI_FIELDS: dict[str, str] = {
    "docker_image": "image",
    "modal_gpu": "gpu",
    "modal_model_volume": "model_volume",
    "modal_app": "app",
}


_AGENT_RESUME_CLI_FIELDS: dict[str, str] = {
    "inner_loop": "inner_loop",
    "interface": "interface",
    "max_retries_per_round": "max_retries_per_round",
    "judge_every": "judge_every",
    "official_eval_every": "official_eval_every",
    "memory_layout": "memory_layout",
}


_PLAIN_RESUME_CLI_FIELDS: dict[str, str] = {
    "max_attempts_per_issue": "max_attempts_per_issue",
    "max_issues_per_perf_eval": "max_issues_per_perf_eval",
}


_EVOLVE_RESUME_CLI_FIELDS: dict[str, str] = {
    "children_per_generation": "children_per_generation",
    "k_top_inspirations": "k_top_inspirations",
    "k_random_inspirations": "k_random_inspirations",
    "selection_temperature": "selection_temperature",
    "seed": "seed",
    "search_policy": "search_policy",
    "openevolve_population_size": "openevolve_population_size",
    "openevolve_archive_size": "openevolve_archive_size",
    "openevolve_num_islands": "openevolve_num_islands",
    "openevolve_migration_interval": "openevolve_migration_interval",
    "openevolve_migration_rate": "openevolve_migration_rate",
    "frontier_bias": "frontier_bias",
    "bootstrap_max_attempts": "bootstrap_max_attempts",
    "keep_deployments": "keep_deployments",
    "max_parallelism": "max_parallelism",
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


_MIGRATE_RUN_ENVIRONMENT_COMMAND = "migrate-run-environment"
