"""Prompt and workspace helpers shared by agent sessions.

The application client owns skill materialization and response parsing. A
session reuses :func:`build_schema_hint` when its provider cannot take the
response schema natively; ``agentshim`` owns the native-schema dialect checks
and materialization.
"""

from __future__ import annotations

import json
import os
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, TextIO

from vs_agent.provider_policy import cli_skill_dirs
from vs_agent.sink import NULL_AGENT_EVENT_SINK
from vs_agent.skills import NULL_SKILL_SELECTION
from vs_project.api.workspace_tree import WorkspacePathError, open_workspace_tree

if TYPE_CHECKING:
    from pathlib import Path

    from pydantic import BaseModel

    from vs_agent.sink import AgentEventSink
    from vs_agent.skills import SkillSelection
    from vs_project.api.workspace_tree import CopyIgnore, WorkspaceTree

# Per-provider CLI skill-discovery paths, matching upstream
# vibesys-skills install.sh conventions. Each CLI tool auto-loads skills from
# a flat directory of `<skill-name>/SKILL.md`. Derived from the shipped
# providers' agentshim profiles (plus the VibeSys-only Cursor addition) so a
# new shipped provider's convention is not hand-copied here.
CLI_SKILL_DIRS: tuple[str, ...] = cli_skill_dirs()


def agent_label(kind: str) -> str:
    """Convert ``"perf_eval"`` to ``"Perf Eval"``, etc."""
    return kind.replace("_", " ").title()


def discover_skill_dirs(root: Path) -> list[Path]:
    """Return all skill directories reachable under *root*.

    A "skill directory" is any directory containing a ``SKILL.md`` file.
    This accepts both flat layouts (``.agents/skills/<name>/SKILL.md``) and
    the tier-organized layout from vibesys-skills
    (``skills/<tier>/<name>/SKILL.md``).
    """
    if (root / "SKILL.md").is_file():
        return [root]
    return [p.parent for p in root.rglob("SKILL.md")]


#: Directories every materialized copy always skips, regardless of caller
#: policy: repo metadata, checked-out sub-repos, and bytecode caches.
_GENERIC_SKIP_NAMES = frozenset({".git", "repos", "__pycache__"})


def materialize_skills(
    workspace: Path,
    skill_dirs: list[Path],
    *,
    selection: SkillSelection = NULL_SKILL_SELECTION,
    log_file: TextIO | None = None,
    event_sink: AgentEventSink = NULL_AGENT_EVENT_SINK,
) -> None:
    """Copy each skill directory into the workspace and CLI discovery paths.

    Walks each ``skill_dirs`` entry for ``SKILL.md`` files and flattens each
    parent directory into the workspace root and every path under
    :data:`CLI_SKILL_DIRS` (one per shipped provider's skill-discovery
    convention, plus the VibeSys-only ``.cursor/skills``). The root copy
    preserves the documented ``<skill-name>/references/...`` paths used by
    prompts and agents, while the hidden copies support native CLI discovery.
    ``selection`` additionally omits whatever directories the caller's policy
    names (e.g. foreign ``references/platforms/<backend>/`` directories) from
    every materialized copy; this function has no opinion on what those are.
    Links are normalized relative to each copied skill, and removed when their
    resolved targets escape the skill or were omitted by the copy policy.

    Existing destinations are replaced on every invocation so skill edits are
    picked up across iterations and after candidate checkpoint rollback. The
    agent may have planted symlinks below its workspace in an earlier turn, so
    every write and removal is walked by descriptor from the workspace root: a
    planted symlink is removed in place of a destination and refused on the way
    to one, never followed. Errors are logged but never raised — the loop should
    still make progress even if a skill fails to materialize.
    """
    if not skill_dirs:
        return

    # Collect every skill dir across all source roots, de-duplicated by name
    # (last writer wins — matches the prior single-source behaviour when the
    # same skill name appears in multiple roots).
    discovered: dict[str, Path] = {}
    for src in skill_dirs:
        for skill_dir in discover_skill_dirs(src):
            discovered[skill_dir.name] = skill_dir

    if not discovered:
        return

    def skip_ignore(src_dir: str, names: list[str]) -> set[str]:
        ignored = {name for name in names if name in _GENERIC_SKIP_NAMES}
        ignored |= selection.skip_dir(src_dir, names)
        return ignored

    def report(message: str) -> None:
        event_sink.agent_output(message + "\n", channel="diagnostic")
        if log_file is not None:
            log_file.write(message + "\n")
            log_file.flush()

    workspace.mkdir(parents=True, exist_ok=True)
    with open_workspace_tree(workspace) as tree:
        for target_rel in (".", *CLI_SKILL_DIRS):
            for failure in _materialize_target(
                tree, workspace, target_rel, discovered, ignore=skip_ignore
            ):
                report(f"[skills] failed to materialize {failure}")


def _materialize_target(
    tree: WorkspaceTree,
    workspace: Path,
    target_rel: str,
    discovered: dict[str, Path],
    *,
    ignore: CopyIgnore,
) -> list[str]:
    """Install every discovered skill below ``target_rel``; return what failed and why."""
    try:
        tree.make_directories(target_rel)
    except (OSError, WorkspacePathError) as exc:
        return [f"into {workspace / target_rel}: {type(exc).__name__}: {exc}"]
    failures: list[str] = []
    for name, src_skill in discovered.items():
        destination = PurePosixPath(target_rel, name).as_posix()
        if src_skill.resolve() == workspace.resolve() / destination:
            continue
        try:
            _materialize_skill_copy(tree, src_skill, destination, ignore=ignore)
        except (OSError, WorkspacePathError) as exc:
            failures.append(
                f"{src_skill} -> {workspace / destination}: {type(exc).__name__}: {exc}"
            )
    return failures


def _materialize_skill_copy(
    tree: WorkspaceTree,
    src_skill: Path,
    destination: str,
    *,
    ignore: CopyIgnore,
) -> None:
    """Replace the copied skill tree at ``destination`` with a fresh copy of ``src_skill``.

    Links are recreated relative to the copy, and only when they resolve, inside the
    source skill, to something the copy kept; foreign, pruned, dangling and cyclic links
    are dropped. Resolving against the source turns absolute links and relative chains
    into canonical targets that contain no source symlinks.
    """
    tree.remove(destination)
    copy = tree.copy_in(src_skill, destination, ignore=ignore)
    source = src_skill.resolve()
    for link in copy.links:
        try:
            target = (source / link).resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if not target.is_relative_to(source):
            continue
        kept = PurePosixPath(target.relative_to(source).as_posix())
        if kept in copy.copied:
            relative_target = os.path.relpath(kept, link.parent)
            tree.symlink(PurePosixPath(destination, link).as_posix(), relative_target)


def build_schema_hint(response_cls: type[BaseModel]) -> str:
    """Render a short instruction telling the CLI tool what JSON to emit."""
    schema = json.dumps(response_cls.model_json_schema(), separators=(",", ":"))
    return (
        "\n\n--\n"
        "Return EXACTLY one JSON object that conforms to the schema below. "
        "Do not wrap it in markdown fences. Do not include any extra prose "
        "before or after the JSON object.\n\n"
        f"Schema for {response_cls.__name__}:\n{schema}\n"
    )
