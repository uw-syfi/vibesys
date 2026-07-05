"""Profiler prompt neutrality tests.

The three per-kind profiler bases (torch / nsys / neuron) describe a GPU/
accelerator *tool*; the *workload* — what to load, which endpoint to drive,
which headline metric to read — belongs in the modality include. These tests
lock that split: a non-text-generation modality (image_generation) render must
carry no text-generation/model workload vocabulary from the base, while the
text_generation render must still carry the relocated capture recipe (a pure
reflow, not a deletion).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from vibe_serve.prompts import render_template

_ROOT = Path(__file__).resolve().parents[3]
_TEMPLATE_DIR = _ROOT / "src" / "vibe_serve" / "loops" / "agent" / "templates"

_PROFILER_TEMPLATES = {
    "torch": "profiler_prompt_torch.j2",
    "nsys": "profiler_prompt_nsys.j2",
    "neuron": "profiler_prompt_neuron.j2",
}

# Text-generation workload/model vocabulary that must never leak from a base
# into a non-text-gen render. GPU-tool words (CUDA, nsys, kernels) are
# intentionally NOT here — they are tool-level and legitimately shared.
_LLM_WORKLOAD_TOKENS = (
    "ML inference server",
    "VibeServeModel",
    ".generate(",
    "max_tokens",
    "max-tokens",
    "median_tok_per_sec",
    "The capital of France",
    "forced tokens",
    "per-decode-step",
)


def _render(kind: str, modality: str, *, env_kind: str = "local") -> str:
    return render_template(
        _PROFILER_TEMPLATES[kind],
        template_dir=_TEMPLATE_DIR,
        profile_focus="",
        bench_path="/workspace/bench",
        modality=modality,
        runtime_notes="",
        env_kind=env_kind,
        objective="OBJECTIVE: maximize throughput.",
    )


@pytest.mark.parametrize("kind", ["torch", "nsys", "neuron"])
def test_non_text_gen_render_has_no_llm_workload_tokens(kind: str):
    """An image_generation render carries none of the text-gen/model workload
    vocabulary, regardless of which GPU base the backend's profiler_kind
    selected — the base itself is workload-neutral."""
    rendered = _render(kind, "image_generation")
    leaked = [tok for tok in _LLM_WORKLOAD_TOKENS if tok in rendered]
    assert not leaked, f"{kind} base leaked text-gen workload tokens into image render: {leaked}"


def test_image_generation_render_keeps_its_own_workload():
    """Neutralizing the base must not strip the modality's own recipe: the
    image_generation include still supplies its diffusion endpoint + workload."""
    rendered = _render("torch", "image_generation")
    assert "/v1/images/generations" in rendered
    assert "A cat sitting on a mat" in rendered


def test_text_generation_torch_render_preserves_relocated_capture_recipe():
    """Relocating Mode A / Modal commands to the modality is a reflow: the
    text_generation render must still contain them."""
    local = _render("torch", "text_generation", env_kind="local")
    assert "VibeServeModel.from_pretrained" in local
    assert "analyze_torch_profile.py capture" in local
    assert "The capital of France is" in local

    modal = _render("torch", "text_generation", env_kind="modal")
    assert "modal run main.py::modal_profile" in modal
    assert "--max-tokens 32" in modal


@pytest.mark.parametrize("kind", ["nsys", "neuron"])
def test_torch_capture_command_does_not_leak_into_non_torch_bases(kind: str):
    """The torch in-process / Modal capture commands are torch-*tool*-specific and
    live in the text_generation modality include; they must be gated on
    profiler_kind so the nsys/neuron bases (which include the same modality file)
    do not render a `torch.profiler` capture recipe their toolchain can't run."""
    for env_kind in ("local", "modal"):
        rendered = _render(kind, "text_generation", env_kind=env_kind)
        assert "analyze_torch_profile.py capture" not in rendered
        assert "modal run main.py::modal_profile" not in rendered


def test_non_text_gen_does_not_inherit_text_gen_capture_command():
    """image_generation rode the torch base's Mode A text-gen command before;
    after relocation it must not receive text-generation capture args."""
    rendered = _render("torch", "image_generation", env_kind="local")
    assert "The capital of France" not in rendered
    assert "--max-tokens" not in rendered


def test_profiler_bases_render_across_modalities_without_error():
    """Every base × modality combination renders (no missing include / undefined)."""
    modalities = ["text_generation", "image_generation", "speech_to_text"]
    for kind in _PROFILER_TEMPLATES:
        for modality in modalities:
            assert _render(kind, modality).strip()
