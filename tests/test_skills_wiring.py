"""Backend-scoped skill sidecar metadata controls which skills are loaded."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from vibe_serve.cli import load_config_and_skills
from vibe_serve.constants import PROJECT_ROOT, ComputeBackend
from vibe_serve.skills import (
    SkillMetadataError,
    discover_sidecar_rules,
    discover_skill_dirs,
    load_sidecar_rules,
    load_skill_frontmatter,
    resolve_skill_source_dirs,
    validate_skill_tree,
)

NKI_WRAPPER_DIR = PROJECT_ROOT / "resources" / "skills" / "neuron-agentic-development"
NKI_SKILL_NAMES = {
    "neuron-nki-debugging",
    "neuron-nki-docs",
    "neuron-nki-profile-querying",
    "neuron-nki-profiling",
    "neuron-nki-writing",
}


def _args(tmp_path, backend, *, no_skills=False, skills_dir=None):
    cfg = tmp_path / "agent.toml"
    cfg.write_text('[model]\nname = "gpt-5.5"\n')
    if skills_dir is None:
        skills_dir = [Path("resources/skills")]
    return SimpleNamespace(config=cfg, no_skills=no_skills, skills_dir=skills_dir, backend=backend)


def _skill_names(skills: list[str] | None) -> set[str]:
    assert skills is not None
    return {Path(s).name for s in skills}


def _write_skill(root: Path, name: str) -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    skill_dir.joinpath("SKILL.md").write_text(
        f"---\nname: {name}\ndescription: test skill\n---\n\n# {name}\n",
        encoding="utf-8",
    )
    return skill_dir


def _write_sidecar(root: Path, content: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / ".vibeserve.toml"
    path.write_text(content, encoding="utf-8")
    return path


def test_trainium_loads_nki_skills_from_sidecar_metadata(tmp_path):
    _, skills, backend = load_config_and_skills(_args(tmp_path, ComputeBackend.TRAINIUM))
    assert backend is ComputeBackend.TRAINIUM
    names = _skill_names(skills)
    assert "serving-systems" in names
    assert NKI_SKILL_NAMES <= names


def test_cuda_filters_out_trainium_scoped_nki_skills(tmp_path):
    _, skills, _ = load_config_and_skills(_args(tmp_path, ComputeBackend.CUDA))
    names = _skill_names(skills)
    assert "serving-systems" in names
    assert names.isdisjoint(NKI_SKILL_NAMES)


def test_no_skills_disables_even_backend_scoped_skills(tmp_path):
    _, skills, _ = load_config_and_skills(_args(tmp_path, ComputeBackend.TRAINIUM, no_skills=True))
    assert skills is None


def test_sidecar_rule_filters_descendant_skill_subtree_by_backend(tmp_path):
    root = tmp_path / "skills"
    _write_skill(root, "portable")
    _write_skill(root / "vendor" / "skills", "trainium-only")
    _write_sidecar(
        root / "vendor",
        '[[rule]]\npath = "skills"\nbackends = ["trainium"]\n',
    )

    cuda = resolve_skill_source_dirs([root], backend=ComputeBackend.CUDA)
    trainium = resolve_skill_source_dirs([root], backend=ComputeBackend.TRAINIUM)

    assert _skill_names(cuda) == {"portable"}
    assert _skill_names(trainium) == {"portable", "trainium-only"}


def test_more_specific_sidecar_rule_wins(tmp_path):
    root = tmp_path / "skills"
    _write_skill(root / "vendor" / "skills" / "common", "cuda-too")
    _write_sidecar(
        root / "vendor",
        '[[rule]]\npath = "skills"\nbackends = ["trainium"]\n',
    )
    _write_sidecar(
        root / "vendor" / "skills" / "common",
        '[[rule]]\npath = "."\nbackends = ["cuda", "trainium"]\n',
    )

    cuda = resolve_skill_source_dirs([root], backend=ComputeBackend.CUDA)
    trainium = resolve_skill_source_dirs([root], backend=ComputeBackend.TRAINIUM)

    assert _skill_names(cuda) == {"cuda-too"}
    assert _skill_names(trainium) == {"cuda-too"}


def test_conflicting_same_specificity_rules_fail(tmp_path):
    root = tmp_path / "skills"
    skill_dir = _write_skill(root / "vendor" / "skills", "ambiguous")
    _write_sidecar(
        root / "vendor",
        '[[rule]]\npath = "skills"\nbackends = ["trainium"]\n',
    )
    rules = discover_sidecar_rules(root / "vendor")
    duplicate_rules = rules + [
        type(rules[0])(
            sidecar_path=rules[0].sidecar_path,
            raw_path=rules[0].raw_path,
            target_path=rules[0].target_path,
            backends=(ComputeBackend.CUDA,),
        )
    ]
    from vibe_serve.skills import effective_skill_metadata

    with pytest.raises(SkillMetadataError, match="conflicting VibeServe rules"):
        effective_skill_metadata(skill_dir, duplicate_rules)


def test_duplicate_skill_dirs_are_deduped(tmp_path):
    root = tmp_path / "skills"
    skill_dir = _write_skill(root, "portable")

    skills = resolve_skill_source_dirs([root, skill_dir], backend=ComputeBackend.CUDA)

    assert [Path(s).name for s in skills] == ["portable"]


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("not toml =\n", "invalid TOML"),
        ('unknown = true\n[[rule]]\npath = "."\n', "unknown top-level key"),
        ("", "expected at least one"),
        ('[[rule]]\npath = "."\nunknown = true\n', "unknown key"),
        ('[[rule]]\npath = "../outside"\n', "must be relative and stay in-tree"),
        ('[[rule]]\npath = "missing"\n', "does not exist"),
        ('[[rule]]\npath = "."\nbackends = "trainium"\n', "`backends` must be a list"),
        (
            '[[rule]]\npath = "."\nbackends = ["trainium", "quantum"]\n',
            "invalid backend name",
        ),
    ],
)
def test_invalid_sidecar_metadata_fails_with_sidecar_path(tmp_path, content, message):
    sidecar = _write_sidecar(tmp_path, content)

    with pytest.raises(SkillMetadataError) as exc:
        load_sidecar_rules(sidecar)

    assert ".vibeserve.toml" in str(exc.value)
    assert message in str(exc.value)


def test_missing_skill_frontmatter_is_invalid(tmp_path):
    skill_dir = tmp_path / "bad-skill"
    skill_dir.mkdir()
    skill_dir.joinpath("SKILL.md").write_text("# bad\n", encoding="utf-8")

    with pytest.raises(SkillMetadataError, match="missing opening YAML frontmatter"):
        load_skill_frontmatter(skill_dir)


def test_all_repository_skill_metadata_is_valid():
    metadata = validate_skill_tree(PROJECT_ROOT / "resources" / "skills")
    names = {m.skill_dir.name for m in metadata}
    assert "serving-systems" in names
    assert NKI_SKILL_NAMES <= names


def test_all_nki_skills_inherit_trainium_scope_from_wrapper_sidecar():
    metadata = {m.skill_dir.name: m for m in validate_skill_tree(NKI_WRAPPER_DIR)}
    assert set(metadata) == NKI_SKILL_NAMES
    assert all(m.backends == (ComputeBackend.TRAINIUM,) for m in metadata.values())


def test_discover_skill_dirs_accepts_single_skill_root(tmp_path):
    skill_dir = _write_skill(tmp_path, "portable")

    assert discover_skill_dirs(skill_dir) == [skill_dir]
