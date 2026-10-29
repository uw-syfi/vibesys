"""The production replay renderer enforces classification before any template can render."""

from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_core.api.replay import ReplayIdentityError, SemanticKey
from vs_prompts.api import TemplateRenderer
from vs_proto.api import core_common
from vs_runtime.api.replay import ReplayPromptRenderer

from .generated.vibesys.runtime.testing.v1.prompt_pb import PromptContext, UnclassifiedContext


def test_renderer_rejects_unclassified_fields_before_template_lookup(tmp_path: Path) -> None:
    renderer = ReplayPromptRenderer(TemplateRenderer(tmp_path))
    with pytest.raises(ReplayIdentityError, match=r"unclassified prompt field.*candidate"):
        renderer.render("absent.j2", UnclassifiedContext(candidate="a"), contract="turn.v1")
    with pytest.raises(ReplayIdentityError, match="must declare replay_context"):
        renderer.render("absent.j2", core_common.Identity(root="a"), contract="turn.v1")


@given(wording=st.text(alphabet=st.characters(blacklist_categories=("Cs",))))
def test_renderer_uses_exact_policy_but_only_candidate_changes_identity(
    wording: str, tmp_path_factory: pytest.TempPathFactory
) -> None:
    root = tmp_path_factory.mktemp("replay-prompts")
    (root / "turn.j2").write_text("{{ candidate }}: {{ instruction }}", encoding="utf-8")
    renderer = ReplayPromptRenderer(TemplateRenderer(root))
    before = renderer.render(
        "turn.j2", PromptContext(candidate="a", instruction="old"), contract="turn.v1"
    )
    after = renderer.render(
        "turn.j2", PromptContext(candidate="a", instruction=wording), contract="turn.v1"
    )
    changed = renderer.render(
        "turn.j2", PromptContext(candidate="b", instruction=wording), contract="turn.v1"
    )
    assert str(after.text) == "a: " + wording
    assert before.context.identity == after.context.identity
    assert after.context.identity != changed.context.identity
    call = after.call(
        SemanticKey("implement"), core_common.Identity, role="implementer", contract="turn.v1"
    )
    assert call.policy.content == after.text.encode()
    assert call.policy.template == "turn.j2"
    assert call.identity == after.context
