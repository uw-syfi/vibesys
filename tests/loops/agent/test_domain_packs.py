"""Tests for domain packs — the ``--domain`` pluggable-context mechanism.

Covers the resolver (built-in name / path / error), the per-role renderer
(present, empty, missing), and end-to-end injection into the base prompts for
both built-in domains (``llm-serving`` carries serving prose; ``generic``
injects nothing of its own).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from vibe_serve.loops.agent.domain import (
    DEFAULT_DOMAIN,
    builtin_domains,
    render_domain_section,
    resolve_domain,
)
from vibe_serve.prompts import render_template

_TEMPLATE_DIR = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "vibe_serve"
    / "loops"
    / "agent"
    / "templates"
)


# --------------------------------------------------------------------------- #
# resolver
# --------------------------------------------------------------------------- #
def test_builtins_present():
    names = builtin_domains()
    assert "llm-serving" in names
    assert "generic" in names
    assert DEFAULT_DOMAIN == "llm-serving"


def test_resolve_builtin_name():
    d = resolve_domain("llm-serving")
    assert d.is_dir()
    assert (d / "domain.md").is_file()


def test_resolve_path(tmp_path: Path):
    (tmp_path / "domain.md").write_text("# Mine\n")
    d = resolve_domain(str(tmp_path))
    assert d == tmp_path.resolve()


def test_resolve_unknown_raises():
    with pytest.raises(ValueError) as exc:
        resolve_domain("does-not-exist-xyz")
    # error lists the built-ins to guide the user
    assert "llm-serving" in str(exc.value)


# --------------------------------------------------------------------------- #
# per-role renderer
# --------------------------------------------------------------------------- #
def test_render_missing_role_is_empty(tmp_path: Path):
    # a domain dir with no <role>.j2 injects nothing
    assert render_domain_section(tmp_path, "implementer") == ""


def test_render_empty_role_is_empty():
    d = resolve_domain("generic")
    for role in ("implementer", "judge", "single_agent"):
        assert render_domain_section(d, role) == ""


def test_render_llm_serving_has_content():
    d = resolve_domain("llm-serving")
    impl = render_domain_section(
        d, "implementer", modality="text_generation", reference_path="/ref"
    )
    assert impl  # non-empty
    # leading/trailing blank lines are stripped — base template owns spacing
    assert impl == impl.strip("\n")


def test_render_role_branches_on_context():
    """A role file rendered with bench_path set should reference it."""
    d = resolve_domain("llm-serving")
    with_bench = render_domain_section(
        d, "judge", modality="text_generation", bench_path="/BENCHX"
    )
    without_bench = render_domain_section(
        d, "judge", modality="text_generation", bench_path=None
    )
    assert "/BENCHX" in with_bench
    assert "/BENCHX" not in without_bench


# --------------------------------------------------------------------------- #
# end-to-end injection into base prompts
# --------------------------------------------------------------------------- #
def _render_implementer(domain: str) -> str:
    d = resolve_domain(domain)
    section = render_domain_section(
        d, "implementer", modality="text_generation", reference_path="/ref"
    )
    return render_template(
        "implementer_prompt.j2",
        template_dir=_TEMPLATE_DIR,
        modality="text_generation",
        domain_implementer=section,
        task="TASK",
        pass_criteria="PC",
        reference_path="/ref",
        runtime_notes="",
        feedback=None,
    )


def test_llm_serving_injects_into_implementer():
    out = _render_implementer("llm-serving")
    # serving-specific prose from the domain pack is present
    assert "serving" in out.lower()
    assert "## Progress tracking" in out  # base skeleton intact


def test_generic_injects_nothing_extra():
    generic = _render_implementer("generic")
    # the only serving refs left are from the modality include, not the domain;
    # the generic render must be strictly shorter than llm-serving's.
    serving = _render_implementer("llm-serving")
    assert len(generic) < len(serving)
    assert "## Progress tracking" in generic  # base skeleton intact


def test_no_triple_blank_at_injection_point():
    """Generic (empty injection) must not leave a triple newline gap."""
    out = _render_implementer("generic")
    # The injection point itself ({% if %}...{% endif %}) must collapse cleanly.
    # Locate the workspace->progress transition that brackets the injection.
    idx = out.index("## Progress tracking")
    window = out[max(0, idx - 6) : idx]
    assert "\n\n\n" not in window
