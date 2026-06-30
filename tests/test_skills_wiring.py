"""Trainium runs auto-include the vendored AWS NKI skills; other backends don't."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from vibe_serve.cli import load_config_and_skills
from vibe_serve.constants import PROJECT_ROOT, ComputeBackend

NKI_DIR = str(PROJECT_ROOT / "resources" / "skills" / "neuron-agentic-development" / "skills")


def _args(tmp_path, backend, *, no_skills=False, skills_dir=None):
    cfg = tmp_path / "agent.toml"
    cfg.write_text('[model]\nname = "gpt-5.5"\n')
    if skills_dir is None:
        skills_dir = [Path("resources/skills/serving-systems")]
    return SimpleNamespace(
        config=cfg, no_skills=no_skills, skills_dir=skills_dir, backend=backend
    )


def test_trainium_auto_includes_nki_skills(tmp_path):
    _, skills, backend = load_config_and_skills(_args(tmp_path, ComputeBackend.TRAINIUM))
    assert backend is ComputeBackend.TRAINIUM
    assert NKI_DIR in skills
    # the default serving-systems skill is still present
    assert any("serving-systems" in s for s in skills)


def test_cuda_does_not_include_nki(tmp_path):
    _, skills, _ = load_config_and_skills(_args(tmp_path, ComputeBackend.CUDA))
    assert NKI_DIR not in skills


def test_no_skills_disables_even_for_trainium(tmp_path):
    _, skills, _ = load_config_and_skills(
        _args(tmp_path, ComputeBackend.TRAINIUM, no_skills=True)
    )
    assert skills is None


def test_trainium_no_duplicate_when_already_present(tmp_path):
    _, skills, _ = load_config_and_skills(
        _args(tmp_path, ComputeBackend.TRAINIUM, skills_dir=[Path(NKI_DIR)])
    )
    assert skills.count(NKI_DIR) == 1
