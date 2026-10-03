"""Golden text for the component ledger that feeds profile-guided prompts.

``ProfileFocus.focus(state).ledger``, rendered by the shared
``focus_ledger`` partial, is read by agents verbatim, so its exact bytes are
pinned per branch. Regenerate with ``UPDATE_PROMPT_SNAPSHOTS=1``
when a wording change is intended.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from vibesys.profile_focus import ProfileFocus, ProfileFocusConfig
from vibesys.profile_focus.state import (
    ProfileFocusState,
    ProfileGuidanceStatus,
    ProfileGuidedComponent,
)
from vibesys.prompts import PROMPTS_DIR, render_template

_GOLDEN_DIR = Path(__file__).parent / "ledger_goldens"

_STATES = {
    "empty": ProfileFocusState(),
    "single_open_no_share": ProfileFocusState(components=[ProfileGuidedComponent(name="attn")]),
    "mixed_statuses": ProfileFocusState(
        active_component="mlp",
        components=[
            ProfileGuidedComponent(name="attn", latest_share=0.123456, rounds_spent=2),
            ProfileGuidedComponent(
                name="mlp",
                status=ProfileGuidanceStatus.ACTIVE,
                latest_share=0.5,
                rounds_spent=1,
                stalled_rounds=1,
            ),
            ProfileGuidedComponent(
                name="kv_cache",
                status=ProfileGuidanceStatus.EXHAUSTED,
                latest_share=0.0,
                rounds_spent=4,
                stalled_rounds=2,
            ),
            ProfileGuidedComponent(name="norm", latest_share=1.0),
        ],
    ),
}


@pytest.mark.parametrize("name", sorted(_STATES))
def test_ledger_text_matches_golden(name: str) -> None:
    ledger = ProfileFocus(ProfileFocusConfig()).focus(_STATES[name]).ledger
    rendered = str(
        render_template(
            "_notices/focus_ledger.j2", template_dir=PROMPTS_DIR / "shared", ledger=ledger
        )
    )
    golden = _GOLDEN_DIR / f"{name}.txt"
    if os.environ.get("UPDATE_PROMPT_SNAPSHOTS") == "1":
        golden.write_text(rendered, encoding="utf-8")
        return
    assert rendered == golden.read_text(encoding="utf-8")
