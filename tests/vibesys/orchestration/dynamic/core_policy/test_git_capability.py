"""A prompt never tells an agent to run a git command its workspace cannot run.

Where an agent's `.git` is read-only (a container mounts the run repository's metadata
read-only), commands that write git metadata fail: commit, cherry-pick, merge, rebase, and
`git apply` with `--3way` or `--index`, which write the index. Reading commands (`git diff`,
`git show`, `git log`, `git merge-base`) and a plain `git apply` to the working tree work
everywhere. The run says which it is with the typed capability ``agent_git_writable``; every
dynamic template renders its instructions from it, and never from a backend's name.
"""

from __future__ import annotations

import re

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.dynamic.core_policy._contexts import (
    CONTEXTS,
    render,
    revision,
)

from vibesys.orchestration.dynamic.search.api import ImplementPrompt, MergePrompt, PromptTemplate
from vs_sim.api.testing import examples

_WRITES = re.compile(
    r"git (?:commit|cherry-pick|rebase|merge(?![-\w])|am|stash|reset|checkout|switch|pull)\b"
    r"|git apply[^`\n]*--(?:3way|index)"
)
"""A git command that writes git metadata (refs, the index or objects)."""

MAIN = revision("rev-main")
PARTS = (
    {"hypothesis_id": "hida", "revision": revision("rev-a1"), "built_on": revision("rev-base")},
    {"hypothesis_id": "hidb", "revision": revision("rev-b2"), "built_on": revision("rev-a1")},
)


def _implement(**fields: object) -> ImplementPrompt:
    base = {
        "hypothesis_id": "hidk",
        "hypothesis": "claim",
        "task": "task",
        "pass_criteria": "faster",
        "parent_revision": MAIN,
    }
    return ImplementPrompt.model_validate({**base, **fields}, strict=False)


def _merge() -> MergePrompt:
    return MergePrompt.model_validate(
        {
            "merge_id": "m1",
            "main_revision": MAIN,
            "candidates": [{"hypothesis_id": "hida", "revision": revision("rev-a1")}],
            "instructions": "apply in order",
            "watch_for": "the scheduler",
        },
        strict=False,
    )


_NAMED = {
    # r4-r7: a continued hypothesis told to `git rebase` onto a moved main, in a container.
    "rebase": lambda: (PromptTemplate.IMPLEMENT, _implement(rebase_onto=MAIN)),
    "rebase-continued": lambda: (
        PromptTemplate.IMPLEMENT,
        _implement(
            rebase_onto=MAIN,
            worktree_revision=revision("rev-k1"),
            prior_revision=revision("rev-k1"),
        ),
    ),
    "combine": lambda: (PromptTemplate.IMPLEMENT, _implement(combine=PARTS)),
    # merge.j2 told the merger that `git cherry-pick` or `git merge` reach each candidate.
    "merge": lambda: (PromptTemplate.MERGE, _merge()),
}

_TEMPLATES = (
    PromptTemplate.IMPLEMENT,
    PromptTemplate.MERGE,
    PromptTemplate.PORTFOLIO,
    PromptTemplate.PORTFOLIO_CORRECTION,
    PromptTemplate.REVIEW,
    PromptTemplate.PROFILE_REQUEST,
    PromptTemplate.OUTLINE,
)


@pytest.mark.regression
@pytest.mark.parametrize("name", sorted(_NAMED))
def test_a_read_only_git_workspace_is_never_told_to_write_git(name: str) -> None:
    template, context = _NAMED[name]()
    text = render(template, context, agent_git_writable=False)

    assert not _WRITES.findall(text), text
    assert "git apply --reject" in text  # the read-only way to take a change, with what to fix


@pytest.mark.parametrize("name", sorted(_NAMED))
def test_a_writable_git_workspace_is_told_the_three_way_apply(name: str) -> None:
    template, context = _NAMED[name]()
    text = render(template, context, agent_git_writable=True)

    assert "git apply --3way" in text
    assert "git apply --reject" not in text


@settings(max_examples=examples(pr=20, nightly=200), deadline=None)
@given(data=st.data(), template=st.sampled_from(_TEMPLATES))
# The reported input: a continued hypothesis told to rebase onto a moved main.
@example(data=None, template=PromptTemplate.IMPLEMENT)
@pytest.mark.regression
def test_no_dynamic_prompt_tells_a_read_only_workspace_to_write_git(
    data: st.DataObject | None, template: PromptTemplate
) -> None:
    context = _implement(rebase_onto=MAIN) if data is None else data.draw(CONTEXTS[template])
    text = render(template, context, agent_git_writable=False)

    assert not _WRITES.findall(text), text
