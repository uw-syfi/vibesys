"""Tests for the helpers shared by the agentshim and Omnigent CLI runners.

These moved out of the pre-driver CLI runner unchanged when the Omnigent
backend needed the same behavior. The cases here cover the branches that both
backends depend on — skill discovery across layouts, platform pruning,
replacement on re-materialization, and the deliberate never-raise policy on
copy failures.
"""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path  # noqa: TC003  # tracked: #288

import pytest

from vibesys.agents.cli_common import (
    CLI_SKILL_DIRS,
    agent_label,
    build_schema_hint,
    discover_skill_dirs,
    materialize_native_output_schema,
    materialize_skills,
)
from vibesys.constants import ComputeBackend
from vibesys.schemas import (
    ImplementerResponse,
    IssuePerfEvalResponse,
    JudgeResponse,
    OrchestratorPlan,
    PerfEvalResponse,
    PerfMetrics,
    PreRoundDecision,
    ProfilerSummary,
    SingleAgentRoundResponse,
)

# Response models with at least one ``dict[str, T]`` field, which Pydantic
# renders as a schema-valued (or ``true``) ``additionalProperties``. Providers
# on the strict profile cannot express these natively; permissive ones can.
MAPPING_RESPONSE_MODELS = [
    ImplementerResponse,
    SingleAgentRoundResponse,
    ProfilerSummary,
    PerfMetrics,
    PerfEvalResponse,
    IssuePerfEvalResponse,
]


def _skill(root: Path, name: str, body: str = "# skill\n") -> Path:
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(body)
    return d


class TestAgentLabel:
    @pytest.mark.parametrize(
        ("kind", "expected"),
        [("perf_eval", "Perf Eval"), ("judge", "Judge"), ("implementer", "Implementer")],
    )
    def test_snake_case_becomes_title_case(self, kind, expected):  # noqa: ANN001, ANN201  # tracked: #288
        assert agent_label(kind) == expected


class TestDiscoverSkillDirs:
    def test_a_root_that_is_itself_a_skill(self, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        (tmp_path / "SKILL.md").write_text("# s\n")

        assert discover_skill_dirs(tmp_path) == [tmp_path]

    def test_flat_layout(self, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        a = _skill(tmp_path, "alpha")
        b = _skill(tmp_path, "beta")

        assert sorted(discover_skill_dirs(tmp_path)) == sorted([a, b])

    def test_tier_organized_layout(self, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        deep = _skill(tmp_path, "tier1/nested")

        assert discover_skill_dirs(tmp_path) == [deep]

    def test_a_root_with_no_skills(self, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        (tmp_path / "notes.txt").write_text("x")

        assert discover_skill_dirs(tmp_path) == []


class TestMaterializeSkills:
    def test_copies_into_every_cli_discovery_path(self, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        src = tmp_path / "src"
        _skill(src, "alpha")
        ws = tmp_path / "ws"
        ws.mkdir()

        materialize_skills(ws, [src])

        assert (ws / "alpha" / "SKILL.md").is_file()
        for rel in CLI_SKILL_DIRS:
            assert (ws / rel / "alpha" / "SKILL.md").is_file()

    def test_no_sources_is_a_noop(self, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        ws = tmp_path / "ws"
        ws.mkdir()

        materialize_skills(ws, [])

        assert list(ws.iterdir()) == []

    def test_sources_without_skills_create_nothing(self, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        src = tmp_path / "src"
        src.mkdir()
        (src / "readme.txt").write_text("x")
        ws = tmp_path / "ws"
        ws.mkdir()

        materialize_skills(ws, [src])

        assert list(ws.iterdir()) == []

    def test_re_materializing_replaces_stale_content(self, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        src = tmp_path / "src"
        _skill(src, "alpha", "# v1\n")
        ws = tmp_path / "ws"
        ws.mkdir()
        materialize_skills(ws, [src])

        (src / "alpha" / "SKILL.md").write_text("# v2\n")
        materialize_skills(ws, [src])

        assert (ws / "alpha/SKILL.md").read_text() == "# v2\n"
        assert (ws / ".claude/skills/alpha/SKILL.md").read_text() == "# v2\n"

    def test_source_already_at_workspace_root_is_not_replaced(self, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        ws = tmp_path / "ws"
        skill = _skill(ws, "alpha", "# local\n")

        materialize_skills(ws, [skill])

        assert (skill / "SKILL.md").read_text() == "# local\n"

    def test_later_source_wins_on_a_name_collision(self, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        first = tmp_path / "a"
        second = tmp_path / "b"
        _skill(first, "dup", "# first\n")
        _skill(second, "dup", "# second\n")
        ws = tmp_path / "ws"
        ws.mkdir()

        materialize_skills(ws, [first, second])

        assert (ws / ".claude/skills/dup/SKILL.md").read_text() == "# second\n"

    def test_a_copy_failure_is_logged_not_raised(self, tmp_path, monkeypatch):  # noqa: ANN001, ANN201  # tracked: #288
        """The loop must still make progress if one skill fails to copy."""
        src = tmp_path / "src"
        _skill(src, "alpha")
        ws = tmp_path / "ws"
        ws.mkdir()
        log = StringIO()

        def _boom(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202  # tracked: #288
            raise OSError("disk on fire")  # noqa: TRY003  # tracked: #288

        monkeypatch.setattr("vibesys.agents.cli_common.shutil.copytree", _boom)

        materialize_skills(ws, [src], log_file=log)

        written = log.getvalue()
        assert "failed to materialize" in written
        assert "disk on fire" in written

    def test_a_copy_failure_without_a_log_is_swallowed(self, tmp_path, monkeypatch):  # noqa: ANN001, ANN201  # tracked: #288
        src = tmp_path / "src"
        _skill(src, "alpha")
        ws = tmp_path / "ws"
        ws.mkdir()
        monkeypatch.setattr(
            "vibesys.agents.cli_common.shutil.copytree",
            lambda *a, **k: (_ for _ in ()).throw(OSError("nope")),  # noqa: ARG005  # tracked: #288
        )

        materialize_skills(ws, [src])  # must not raise

    def test_a_stale_symlink_destination_is_replaced(self, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        src = tmp_path / "src"
        _skill(src, "alpha")
        ws = tmp_path / "ws"
        dest = ws / ".claude/skills"
        dest.mkdir(parents=True)
        (dest / "alpha").symlink_to(tmp_path / "does-not-exist")

        materialize_skills(ws, [src])

        assert (dest / "alpha" / "SKILL.md").is_file()
        assert not (dest / "alpha").is_symlink()


class TestPlatformPruning:
    """``compute_backend`` selects which ``references/platforms/`` trees survive."""

    @staticmethod
    def _materialize(tmp_path: Path, compute_backend: ComputeBackend | None) -> Path:
        """Materialize a skill carrying ``references/platforms/<backend>/`` trees."""
        skill_src = tmp_path / "serving-systems"
        skill_src.mkdir()
        (skill_src / "SKILL.md").write_text("# serving-systems\n")
        # Portable tier — must survive on every backend.
        algorithms = skill_src / "references" / "algorithms"
        algorithms.mkdir(parents=True)
        (algorithms / "continuous-batching.md").write_text("# contract\n")
        # One directory per backend under references/platforms/.
        for backend in ComputeBackend:
            platform = skill_src / "references" / "platforms" / backend.value
            platform.mkdir(parents=True)
            (platform / "floor.md").write_text(f"# {backend.value} floor\n")
        # A same-named directory *outside* references/platforms/ must not be
        # pruned — pruning keys on the parent path, not the bare name.
        decoy = skill_src / "references" / "models" / "cuda"
        decoy.mkdir(parents=True)
        (decoy / "note.md").write_text("# decoy\n")

        ws = tmp_path / "ws"
        ws.mkdir()
        materialize_skills(ws, [skill_src], compute_backend=compute_backend)
        return ws / ".claude/skills" / "serving-systems" / "references"

    @pytest.mark.parametrize(
        "selected", [ComputeBackend.CUDA, ComputeBackend.TRAINIUM, ComputeBackend.METAL]
    )
    def test_prunes_foreign_platform_dirs(self, tmp_path, selected):  # noqa: ANN001, ANN201  # tracked: #288
        """Only the selected backend's platform guidance reaches the agent.

        Applying one platform's floor to another produces wrong work — the
        CUDA guidance to eliminate KV padding is inverted on Trainium — so the
        foreign trees must be absent, not merely deprioritized.
        """
        refs = self._materialize(tmp_path, selected)

        platforms = refs / "platforms"
        assert {p.name for p in platforms.iterdir()} == {selected.value}
        assert (platforms / selected.value / "floor.md").exists()

    def test_keeps_portable_tiers_and_same_named_dirs(self, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        """Pruning is scoped to references/platforms/, not to directory names."""
        refs = self._materialize(tmp_path, ComputeBackend.TRAINIUM)

        assert (refs / "algorithms" / "continuous-batching.md").exists()
        # `models/cuda/` shares a name with a backend but is not a platform dir.
        assert (refs / "models" / "cuda" / "note.md").exists()

    def test_without_a_backend_every_platform_is_kept(self, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        """No selected backend (e.g. non-run tooling) copies the tree intact."""
        refs = self._materialize(tmp_path, None)

        assert {p.name for p in (refs / "platforms").iterdir()} == {b.value for b in ComputeBackend}


class TestBuildSchemaHint:
    def test_names_the_model_and_embeds_its_schema(self):  # noqa: ANN201  # tracked: #288
        hint = build_schema_hint(JudgeResponse)

        assert "JudgeResponse" in hint
        assert "EXACTLY one JSON object" in hint
        assert "verdict" in hint

    def test_forbids_markdown_fences(self):  # noqa: ANN201  # tracked: #288
        """CLI tools wrap JSON in fences unless told not to."""
        assert "Do not wrap it in markdown fences" in build_schema_hint(JudgeResponse)


def _assert_declared_objects_are_closed(node) -> None:  # noqa: ANN001  # tracked: #288
    """Every object that declares properties requires all of them and is closed."""
    if isinstance(node, list):
        for value in node:
            _assert_declared_objects_are_closed(value)
    elif isinstance(node, dict):
        if "$ref" in node:
            assert set(node) == {"$ref"}
        if "properties" in node:
            assert set(node["required"]) == set(node["properties"])
        for value in node.values():
            _assert_declared_objects_are_closed(value)


def _open_maps(node) -> list:  # noqa: ANN001  # tracked: #288
    """Collect every ``additionalProperties`` value that permits extra keys."""
    found = []
    if isinstance(node, list):
        for value in node:
            found.extend(_open_maps(value))
    elif isinstance(node, dict):
        additional = node.get("additionalProperties")
        if additional not in (None, False):
            found.append(additional)
        for value in node.values():
            found.extend(_open_maps(value))
    return found


class TestMaterializeNativeOutputSchema:
    @pytest.mark.parametrize(
        "response_cls",
        [PreRoundDecision, OrchestratorPlan, JudgeResponse],
    )
    def test_active_agent_schemas_materialize_atomically(self, tmp_path, response_cls):  # noqa: ANN001, ANN201  # tracked: #288
        relative = materialize_native_output_schema(tmp_path, response_cls)
        target = tmp_path / relative
        schema = json.loads(target.read_text())

        def assert_strict_objects(node):  # noqa: ANN001, ANN202  # tracked: #288
            if isinstance(node, list):
                for value in node:
                    assert_strict_objects(value)
            elif isinstance(node, dict):
                if "$ref" in node:
                    assert set(node) == {"$ref"}
                if "properties" in node:
                    assert set(node["required"]) == set(node["properties"])
                    assert node["additionalProperties"] is False
                for value in node.values():
                    assert_strict_objects(value)

        assert relative.startswith(".cache/vibesys/response-schemas/")
        assert_strict_objects(schema)
        assert "default" not in target.read_text()
        assert not list(target.parent.glob(f".{target.name}.*"))

    @pytest.mark.parametrize("response_cls", MAPPING_RESPONSE_MODELS)
    def test_strict_profile_rejects_mappings_before_writing(self, tmp_path, response_cls):  # noqa: ANN001, ANN201  # tracked: #288
        """Providers on the Codex subset keep the prompt fallback for these."""
        with pytest.raises(ValueError, match="arbitrary object keys"):
            materialize_native_output_schema(tmp_path, response_cls)

        assert not (tmp_path / ".cache/vibesys/response-schemas").exists()

    @pytest.mark.parametrize("response_cls", MAPPING_RESPONSE_MODELS)
    def test_permissive_profile_materializes_mappings(self, tmp_path, response_cls):  # noqa: ANN001, ANN201  # tracked: #288
        relative = materialize_native_output_schema(
            tmp_path, response_cls, allow_arbitrary_keys=True
        )
        target = tmp_path / relative
        schema = json.loads(target.read_text())

        assert relative.startswith(".cache/vibesys/response-schemas/")
        _assert_declared_objects_are_closed(schema)
        assert "default" not in target.read_text()
        assert not list(target.parent.glob(f".{target.name}.*"))
        # The mapping is preserved rather than rewritten to ``false``, so the
        # CLI still constrains the value type of each arbitrary key.
        assert _open_maps(schema), "expected the arbitrary-key mapping to survive"

    @pytest.mark.parametrize("response_cls", [PreRoundDecision, OrchestratorPlan, JudgeResponse])
    def test_permissive_profile_still_closes_declared_objects(self, tmp_path, response_cls):  # noqa: ANN001, ANN201  # tracked: #288
        """Relaxing the mapping rule must not open up ordinary models."""
        relative = materialize_native_output_schema(
            tmp_path, response_cls, allow_arbitrary_keys=True
        )
        schema = json.loads((tmp_path / relative).read_text())

        assert _open_maps(schema) == []

    def test_permissive_profile_validates_inside_a_mapping(self, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        """The preserved mapping subschema is still walked for unsupported constructs."""

        class MappedUnsupportedResponse(JudgeResponse):
            @classmethod
            def model_json_schema(cls, *args, **kwargs):  # noqa: ANN002, ANN003, ANN206, ARG003  # tracked: #288
                return {
                    "type": "object",
                    "properties": {"metrics": {"type": "object"}},
                    "additionalProperties": {"oneOf": [{"type": "string"}]},
                }

        with pytest.raises(ValueError, match="unsupported keyword 'oneOf'"):
            materialize_native_output_schema(
                tmp_path, MappedUnsupportedResponse, allow_arbitrary_keys=True
            )

        assert not (tmp_path / ".cache/vibesys/response-schemas").exists()

    def test_rejects_unsupported_schema_constructs_before_writing(self, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        class UnsupportedResponse(JudgeResponse):
            @classmethod
            def model_json_schema(cls, *args, **kwargs):  # noqa: ANN002, ANN003, ANN206, ARG003  # tracked: #288
                return {
                    "type": "object",
                    "properties": {"analysis": {"type": "string"}},
                    "not": {"required": ["analysis"]},
                }

        with pytest.raises(ValueError, match="unsupported keyword 'not'"):
            materialize_native_output_schema(tmp_path, UnsupportedResponse)

        assert not (tmp_path / ".cache/vibesys/response-schemas").exists()
