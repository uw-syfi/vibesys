"""The chat agent's bundled skills: a tree of their own, delivered to the chat agent only."""

from __future__ import annotations

import re

from launch.chat_tools import PROPOSE_STEER_TOOL, READ_TOOLS
from server.chat.prompts import experiment_chat_system_prompt
from vibesys.api import AuxiliaryAgentLaunch, AuxiliarySkills
from vibesys.api.request import default_skill_roots
from vibesys.api.wiring import chat_skill_dirs
from vs_runtime.api.infrastructure import discover_skill_dirs, load_skill_frontmatter

CHAT_SKILLS = {"reading-ideas", "results-against-base", "run-roles", "steering-a-run"}


def test_the_chat_skills_are_the_bundled_chat_tree() -> None:
    assert {path.name for path in chat_skill_dirs()} == CHAT_SKILLS


def test_every_chat_skill_has_valid_frontmatter_naming_its_directory() -> None:
    for skill_dir in chat_skill_dirs():
        frontmatter = load_skill_frontmatter(skill_dir)
        assert frontmatter["name"] == skill_dir.name
        assert str(frontmatter["description"]).strip()


def test_optimization_agents_never_discover_a_chat_skill() -> None:
    discovered = {
        skill.name for root in default_skill_roots() for skill in discover_skill_dirs(root)
    }

    assert discovered.isdisjoint(CHAT_SKILLS)


def test_a_launch_asks_for_the_run_skills_unless_it_names_the_chat_set() -> None:
    launch = AuxiliaryAgentLaunch(
        role="chat", member_id="default", provider="codex", model="m", system_prompt="p"
    )

    assert launch.skills is AuxiliarySkills.RUN
    assert launch.model_copy(update={"skills": AuxiliarySkills.CHAT}).skills.value == "chat"


def test_every_tool_the_prompt_and_skills_name_is_one_the_server_offers() -> None:
    offered = {*READ_TOOLS, PROPOSE_STEER_TOOL}
    texts = [experiment_chat_system_prompt()]
    texts.extend((skill / "SKILL.md").read_text(encoding="utf-8") for skill in chat_skill_dirs())
    named = {
        name
        for text in texts
        for name in re.findall(r"`((?:list|get|read|run|propose)_[a-z_]+)(?:\b|[ (`])", text)
    }

    assert named
    assert named <= offered
    assert PROPOSE_STEER_TOOL in named
