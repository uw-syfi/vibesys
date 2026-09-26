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


_RUN_ENVIRONMENT_OPTION_CLI_FIELDS: dict[str, str] = {
    "docker_image": "image",
    "modal_gpu": "gpu",
    "modal_model_volume": "model_volume",
    "modal_app": "app",
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
