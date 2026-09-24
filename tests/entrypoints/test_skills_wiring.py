"""Domain- and backend-scoped metadata controls which skills are loaded."""

from __future__ import annotations

import argparse
import re
from collections.abc import Iterable
from pathlib import Path

import pytest

from entrypoints.cli import load_config_and_skills
from vibesys.constants import PROJECT_ROOT, ComputeBackend, DomainName
from vibesys.schemas import SkillResourceSelection
from vibesys.skills import (
    PLATFORM_SKELETON,
    SkillMetadataError,
    _is_in_hidden_dir,
    build_skill_catalog,
    coerce_skill_root,
    discover_sidecar_rules,
    discover_skill_dirs,
    effective_skill_metadata,
    load_sidecar_rules,
    load_skill_frontmatter,
    resolve_skill_selections,
    resolve_skill_source_dirs,
    validate_platform_layout,
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


def _args(
    tmp_path: Path,
    backend: ComputeBackend,
    *,
    no_skills: bool = False,
    skills_dir: list[Path] | None = None,
    extra_skills: list[Path] | None = None,
    default_presets: bool = True,
) -> argparse.Namespace:
    cfg = tmp_path / "agent.toml"
    cfg.write_text('[model]\nname = "gpt-5.5"\n')
    # By default emulate "presets only" by pointing --skills-dir at the repo
    # presets; pass default_presets=False to exercise the omitted-flag path.
    if skills_dir is None and default_presets:
        skills_dir = [Path("resources/skills")]
    return argparse.Namespace(
        config=cfg,
        no_skills=no_skills,
        skills_dir=skills_dir,
        extra_skills=extra_skills,
        backend=backend,
    )


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
    path = root / ".vibesys.toml"
    path.write_text(content, encoding="utf-8")
    return path


def test_trainium_loads_nki_skills_from_sidecar_metadata(tmp_path: Path) -> None:
    _, skills, backend = load_config_and_skills(
        _args(tmp_path, ComputeBackend.TRAINIUM),
        domain=DomainName.LLM_SERVING,
    )
    assert backend is ComputeBackend.TRAINIUM
    names = _skill_names(skills)
    assert "serving-systems" in names
    assert names >= NKI_SKILL_NAMES


def test_cuda_filters_out_trainium_scoped_nki_skills(tmp_path: Path) -> None:
    _, skills, _ = load_config_and_skills(
        _args(tmp_path, ComputeBackend.CUDA),
        domain=DomainName.LLM_SERVING,
    )
    names = _skill_names(skills)
    assert "serving-systems" in names
    assert names.isdisjoint(NKI_SKILL_NAMES)


@pytest.mark.parametrize("domain", [DomainName.GENERIC, DomainName.MICROSERVICES])
def test_non_serving_domains_filter_out_serving_systems(tmp_path: Path, domain: DomainName) -> None:
    _, skills, _ = load_config_and_skills(_args(tmp_path, ComputeBackend.CUDA), domain=domain)

    assert "serving-systems" not in _skill_names(skills)


def test_no_skills_disables_even_compatible_skills(tmp_path: Path) -> None:
    _, skills, _ = load_config_and_skills(
        _args(tmp_path, ComputeBackend.TRAINIUM, no_skills=True),
        domain=DomainName.LLM_SERVING,
    )
    assert skills is None


def test_omitted_skills_dir_uses_presets(tmp_path: Path) -> None:
    # With no --skills-dir the preset roots (resources/skills) are the base.
    _, skills, _ = load_config_and_skills(
        _args(tmp_path, ComputeBackend.CUDA, default_presets=False),
        domain=DomainName.LLM_SERVING,
    )
    assert "serving-systems" in _skill_names(skills)


def test_extra_skills_stack_on_presets(tmp_path: Path) -> None:
    custom_root = tmp_path / "mine"
    _write_skill(custom_root, "custom-skill")

    _, skills, _ = load_config_and_skills(
        _args(tmp_path, ComputeBackend.CUDA, extra_skills=[custom_root]),
        domain=DomainName.LLM_SERVING,
    )
    names = _skill_names(skills)
    assert "custom-skill" in names  # user skill loaded
    assert "serving-systems" in names  # preset still present


def test_skills_dir_replaces_presets(tmp_path: Path) -> None:
    custom_root = tmp_path / "mine"
    _write_skill(custom_root, "custom-skill")

    _, skills, _ = load_config_and_skills(
        _args(tmp_path, ComputeBackend.CUDA, skills_dir=[custom_root]),
        domain=DomainName.LLM_SERVING,
    )
    names = _skill_names(skills)
    assert names == {"custom-skill"}  # only the override, presets replaced


def test_coerce_skill_root_accepts_skill_md_file(tmp_path: Path) -> None:
    skill_dir = _write_skill(tmp_path, "portable")

    assert coerce_skill_root(skill_dir / "SKILL.md") == skill_dir.resolve()

    (tmp_path / "notes.md").write_text("x\n")
    with pytest.raises(ValueError, match="must be a SKILL"):
        coerce_skill_root(tmp_path / "notes.md")


def test_resolve_accepts_single_skill_md_file(tmp_path: Path) -> None:
    root = tmp_path / "mine"
    skill_dir = _write_skill(root, "one")
    _write_skill(root, "two")  # sibling that must NOT be pulled in

    resolved = resolve_skill_source_dirs(
        [skill_dir / "SKILL.md"], backend=ComputeBackend.CUDA, domain=DomainName.GENERIC
    )

    assert _skill_names(resolved) == {"one"}


def test_sidecar_rule_filters_descendant_skill_subtree_by_backend(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _write_skill(root, "portable")
    _write_skill(root / "vendor" / "skills", "trainium-only")
    _write_sidecar(
        root / "vendor",
        '[[rule]]\npath = "skills"\nbackends = ["trainium"]\n',
    )

    cuda = resolve_skill_source_dirs([root], backend=ComputeBackend.CUDA, domain=DomainName.GENERIC)
    trainium = resolve_skill_source_dirs(
        [root], backend=ComputeBackend.TRAINIUM, domain=DomainName.GENERIC
    )

    assert _skill_names(cuda) == {"portable"}
    assert _skill_names(trainium) == {"portable", "trainium-only"}


def test_sidecar_rule_filters_skill_by_domain(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _write_skill(root, "portable")
    _write_skill(root, "serving-only")
    _write_sidecar(
        root,
        '[[rule]]\npath = "serving-only"\ndomains = ["llm-serving"]\n',
    )

    generic = resolve_skill_source_dirs(
        [root], backend=ComputeBackend.CUDA, domain=DomainName.GENERIC
    )
    serving = resolve_skill_source_dirs(
        [root], backend=ComputeBackend.CUDA, domain=DomainName.LLM_SERVING
    )

    assert _skill_names(generic) == {"portable"}
    assert _skill_names(serving) == {"portable", "serving-only"}


def test_more_specific_sidecar_rule_wins(tmp_path: Path) -> None:
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

    cuda = resolve_skill_source_dirs([root], backend=ComputeBackend.CUDA, domain=DomainName.GENERIC)
    trainium = resolve_skill_source_dirs(
        [root], backend=ComputeBackend.TRAINIUM, domain=DomainName.GENERIC
    )

    assert _skill_names(cuda) == {"cuda-too"}
    assert _skill_names(trainium) == {"cuda-too"}


def test_conflicting_same_specificity_rules_fail(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    skill_dir = _write_skill(root / "vendor" / "skills", "ambiguous")
    _write_sidecar(
        root / "vendor",
        '[[rule]]\npath = "skills"\nbackends = ["trainium"]\n',
    )
    rules = discover_sidecar_rules(root / "vendor")
    duplicate_rules = [
        *rules,
        type(rules[0])(
            sidecar_path=rules[0].sidecar_path,
            raw_path=rules[0].raw_path,
            target_path=rules[0].target_path,
            backends=(ComputeBackend.CUDA,),
            domains=None,
        ),
    ]

    with pytest.raises(SkillMetadataError, match="conflicting VibeSys rules"):
        effective_skill_metadata(skill_dir, duplicate_rules)


def test_duplicate_skill_dirs_are_deduped(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    skill_dir = _write_skill(root, "portable")

    skills = resolve_skill_source_dirs(
        [root, skill_dir], backend=ComputeBackend.CUDA, domain=DomainName.GENERIC
    )

    assert [Path(s).name for s in skills] == ["portable"]


def test_discovery_ignores_hidden_skill_directories(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    visible = _write_skill(root, "portable")
    hidden = root / ".claude" / "skills" / "foreign"
    hidden.mkdir(parents=True)
    hidden.joinpath("SKILL.md").write_text("# upstream skill without frontmatter\n")

    assert discover_skill_dirs(root) == [visible]
    assert resolve_skill_source_dirs(
        [root], backend=ComputeBackend.CUDA, domain=DomainName.GENERIC
    ) == [str(visible)]


def test_hidden_dir_check_treats_external_paths_as_visible(tmp_path: Path) -> None:
    assert not _is_in_hidden_dir(tmp_path / "other" / ".hidden" / "SKILL.md", tmp_path / "skills")


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
        ('[[rule]]\npath = "."\ndomains = "llm-serving"\n', "`domains` must be a list"),
        (
            '[[rule]]\npath = "."\ndomains = ["llm-serving", "quantum"]\n',
            "invalid domain name",
        ),
    ],
)
def test_invalid_sidecar_metadata_fails_with_sidecar_path(
    tmp_path: Path, content: str, message: str
) -> None:
    sidecar = _write_sidecar(tmp_path, content)

    with pytest.raises(SkillMetadataError) as exc:
        load_sidecar_rules(sidecar)

    assert ".vibesys.toml" in str(exc.value)
    assert message in str(exc.value)


def test_missing_skill_frontmatter_is_invalid(tmp_path: Path) -> None:
    skill_dir = tmp_path / "bad-skill"
    skill_dir.mkdir()
    skill_dir.joinpath("SKILL.md").write_text("# bad\n", encoding="utf-8")

    with pytest.raises(SkillMetadataError, match="missing opening YAML frontmatter"):
        load_skill_frontmatter(skill_dir)


def test_all_repository_skill_metadata_is_valid() -> None:
    metadata = {
        item.skill_dir.name: item
        for item in validate_skill_tree(PROJECT_ROOT / "resources" / "skills")
    }
    assert metadata["serving-systems"].domains == (DomainName.LLM_SERVING,)
    assert set(metadata) >= NKI_SKILL_NAMES


def test_all_nki_skills_inherit_trainium_scope_from_wrapper_sidecar() -> None:
    metadata = {m.skill_dir.name: m for m in validate_skill_tree(NKI_WRAPPER_DIR)}
    assert set(metadata) == NKI_SKILL_NAMES
    assert all(m.backends == (ComputeBackend.TRAINIUM,) for m in metadata.values())
    assert all(m.domains is None for m in metadata.values())


def test_discover_skill_dirs_accepts_single_skill_root(tmp_path: Path) -> None:
    skill_dir = _write_skill(tmp_path, "portable")

    assert discover_skill_dirs(skill_dir) == [skill_dir]


# -- references/platforms/ layout ------------------------------------------


def _write_platform(
    skill_dir: Path,
    backend: str,
    *,
    skeleton: Iterable[str] = PLATFORM_SKELETON,
) -> Path:
    platform_dir = skill_dir / "references" / "platforms" / backend
    platform_dir.mkdir(parents=True)
    for name in skeleton:
        platform_dir.joinpath(name).write_text(f"# {backend} {name}\n", encoding="utf-8")
    return platform_dir


def test_skill_without_platforms_tree_still_validates(tmp_path: Path) -> None:
    skill_dir = _write_skill(tmp_path, "portable")

    validate_platform_layout(skill_dir)  # no references/platforms/ — nothing to check


def test_complete_platform_skeleton_validates(tmp_path: Path) -> None:
    skill_dir = _write_skill(tmp_path, "multi")
    _write_platform(skill_dir, "cuda")
    _write_platform(skill_dir, "trainium")

    validate_platform_layout(skill_dir)


@pytest.mark.parametrize("missing", PLATFORM_SKELETON)
def test_platform_missing_a_skeleton_file_fails(tmp_path: Path, missing: str) -> None:
    skill_dir = _write_skill(tmp_path, "gappy")
    _write_platform(skill_dir, "metal", skeleton=[n for n in PLATFORM_SKELETON if n != missing])

    with pytest.raises(SkillMetadataError, match=f"missing required file.*{missing}"):
        validate_platform_layout(skill_dir)


def test_platform_dir_must_be_a_known_compute_backend(tmp_path: Path) -> None:
    skill_dir = _write_skill(tmp_path, "typo")
    _write_platform(skill_dir, "nvidia")  # vendor name, not a ComputeBackend value

    with pytest.raises(SkillMetadataError, match="unknown platform director"):
        validate_platform_layout(skill_dir)


def test_validate_skill_tree_enforces_platform_layout(tmp_path: Path) -> None:
    skill_dir = _write_skill(tmp_path, "gappy")
    _write_platform(skill_dir, "cuda", skeleton=["floor.md"])

    with pytest.raises(SkillMetadataError, match="missing required file"):
        validate_skill_tree(tmp_path)


def test_serving_systems_platform_dirs_cover_every_compute_backend() -> None:
    """Every backend a run can select must have its own guidance.

    Without this, selecting a backend silently falls back to reading another
    platform's floor — which is wrong work, not merely missing work.
    """
    platforms = (
        PROJECT_ROOT / "resources" / "skills" / "serving-systems" / "references" / "platforms"
    )
    present = {p.name for p in platforms.iterdir() if p.is_dir()}

    assert present == {backend.value for backend in ComputeBackend}


# -- link discipline: portable tiers must not link into platforms/ ----------

SERVING_SYSTEMS = PROJECT_ROOT / "resources" / "skills" / "serving-systems"
PORTABLE_TIERS = ("algorithms", "models", "tooling", "frameworks", "engines")
# `](...)` targets that resolve into a platform directory.
_PLATFORM_LINK = re.compile(r"\]\((?:\.{1,2}/)*platforms/[a-z0-9]+/[^)]+\)")


def _portable_reference_files() -> list[Path]:
    refs = SERVING_SYSTEMS / "references"
    return sorted(f for tier in PORTABLE_TIERS for f in (refs / tier).rglob("*.md"))


def test_portable_references_never_link_into_a_platform_dir() -> None:
    """Materialization prunes every non-selected platform directory, so a
    markdown link from a portable file into ``platforms/<backend>/<file>``
    dangles on every other backend. Link the directory or name the library as
    plain text instead.
    """
    offenders = {
        f.relative_to(SERVING_SYSTEMS): _PLATFORM_LINK.findall(f.read_text(encoding="utf-8"))
        for f in _portable_reference_files()
    }
    offenders = {path: hits for path, hits in offenders.items() if hits}

    assert not offenders, "portable references must not deep-link into platforms/: " + "; ".join(
        f"{path} -> {hits}" for path, hits in offenders.items()
    )


def test_portable_references_have_no_links_to_removed_tiers() -> None:
    """`backends/` and `hardware/` dissolved into `platforms/`; a leftover link
    to either is a dead path in every workspace, not just foreign ones."""
    stale = re.compile(r"\]\((?:\.{1,2}/)*(?:backends|hardware)/[^)]*\)")
    offenders = {
        f.relative_to(SERVING_SYSTEMS): stale.findall(f.read_text(encoding="utf-8"))
        for f in (SERVING_SYSTEMS / "references").rglob("*.md")
    }
    offenders = {path: hits for path, hits in offenders.items() if hits}

    assert not offenders, "links to removed tiers: " + "; ".join(
        f"{path} -> {hits}" for path, hits in offenders.items()
    )


def _strip_code_blocks(text: str) -> str:
    """Drop fenced blocks so kernel-launch syntax like ``k[grid](x, y)`` isn't
    mistaken for a markdown link."""
    out, fenced = [], False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if not fenced:
            out.append(line)
    return "\n".join(out)


def test_serving_systems_internal_links_resolve() -> None:
    """Every relative markdown link inside the skill points at a real file.

    ``CLAUDE.md`` is excluded: as the authoring guide it cites illustrative
    paths (``references/<topic>.md``) and one deliberate counter-example of a
    link the discipline forbids.
    """
    broken: list[str] = []
    link = re.compile(r"\]\((?!https?://|#)([^)]+)\)")
    for md in SERVING_SYSTEMS.rglob("*.md"):
        rel = md.relative_to(SERVING_SYSTEMS)
        if "repos" in rel.parts or rel.name == "CLAUDE.md":
            continue
        body = _strip_code_blocks(md.read_text(encoding="utf-8"))
        for target in link.findall(body):
            path = target.split("#", 1)[0]
            if not path or path.startswith("$"):
                continue
            if not (md.parent / path).exists():
                broken.append(f"{rel} -> {target}")

    assert not broken, "broken internal links:\n" + "\n".join(broken)


def test_skill_selection_resolves_zero_to_many_resources_with_stable_deduplication(
    tmp_path: Path,
) -> None:
    root = tmp_path / "skills"
    skill = _write_skill(root, "portable")
    references = skill / "references"
    references.mkdir()
    references.joinpath("design.md").write_text("design\n")
    scripts = skill / "scripts"
    scripts.mkdir()
    scripts.joinpath("probe.sh").write_text("#!/bin/sh\n")

    catalog = build_skill_catalog([root])
    resolved, diagnostics = resolve_skill_selections(
        [
            SkillResourceSelection(
                skill="portable",
                resource_paths=["references/design.md", "references/design.md"],
                purpose="Design the change.",
            ),
            SkillResourceSelection(
                skill="portable",
                resource_paths=["scripts/probe.sh", "SKILL.md"],
                purpose="Run the probe.",
            ),
        ],
        catalog,
    )

    assert diagnostics == []
    assert len(resolved) == 1
    assert resolved[0].router_path == "portable/SKILL.md"
    assert resolved[0].resource_paths == (
        "portable/references/design.md",
        "portable/scripts/probe.sh",
    )
    assert resolved[0].purpose == "Design the change."
    assert resolve_skill_selections([], catalog) == ([], [])


def test_skill_selection_omits_unknown_skills_and_unsafe_resources(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    skill = _write_skill(root, "portable")
    references = skill / "references"
    references.mkdir()
    references.joinpath("valid.md").write_text("valid\n")
    (skill / "repos").mkdir()
    (skill / "repos" / "hidden.md").write_text("hidden\n")

    catalog = build_skill_catalog([root])
    resolved, diagnostics = resolve_skill_selections(
        [
            SkillResourceSelection(
                skill="missing",
                purpose="Not installed.",
            ),
            SkillResourceSelection(
                skill="portable",
                resource_paths=[
                    "references/valid.md",
                    "../outside.md",
                    "/absolute.md",
                    "references",
                    "references/missing.md",
                    "repos/hidden.md",
                    "references\\valid.md",
                ],
                purpose="Keep valid siblings.",
            ),
        ],
        catalog,
    )

    assert len(resolved) == 1
    assert resolved[0].resource_paths == ("portable/references/valid.md",)
    assert len(diagnostics) == 7
    assert "unknown installed skill" in diagnostics[0]
    assert any("stay within" in diagnostic for diagnostic in diagnostics)
    assert any("must identify a file" in diagnostic for diagnostic in diagnostics)
    assert any("does not exist" in diagnostic for diagnostic in diagnostics)
    assert any("excluded" in diagnostic for diagnostic in diagnostics)
    assert any("POSIX separators" in diagnostic for diagnostic in diagnostics)


def test_skill_selection_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    skill = _write_skill(root, "portable")
    references = skill / "references"
    references.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("outside\n")
    references.joinpath("escape.md").symlink_to(outside)

    resolved, diagnostics = resolve_skill_selections(
        [
            SkillResourceSelection(
                skill="portable",
                resource_paths=["references/escape.md"],
                purpose="Attempt escape.",
            )
        ],
        build_skill_catalog([root]),
    )

    assert len(resolved) == 1
    assert resolved[0].resource_paths == ()
    assert len(diagnostics) == 1
    assert "escapes the skill root" in diagnostics[0]


def test_skill_catalog_matches_materialization_last_writer_wins(tmp_path: Path) -> None:
    first = _write_skill(tmp_path / "first", "portable")
    second = _write_skill(tmp_path / "second", "portable")

    catalog = build_skill_catalog([first, second])

    assert catalog["portable"].source_dir == second.resolve()


def test_skill_catalog_rejects_frontmatter_name_that_cannot_be_materialized(tmp_path: Path) -> None:
    skill = _write_skill(tmp_path, "portable")
    skill.joinpath("SKILL.md").write_text(
        "---\nname: different\ndescription: mismatch\n---\n",
        encoding="utf-8",
    )

    with pytest.raises(SkillMetadataError, match="must match directory name"):
        build_skill_catalog([skill])
