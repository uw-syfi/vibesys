"""Prompt and workspace helpers shared by external-agent drivers.

The application client owns skill materialization and response parsing. A
driver reuses :func:`build_schema_hint` when its provider cannot take the
response schema natively; ``agentshim`` owns the native-schema dialect checks
and materialization.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path  # noqa: TC003  # tracked: #288
from typing import TYPE_CHECKING, TextIO

from pydantic import BaseModel  # noqa: TC002  # tracked: #288

from vs_agent.provider_policy import cli_skill_dirs
from vs_agent.sink import NULL_AGENT_EVENT_SINK
from vs_agent.skills import NULL_SKILL_SELECTION

if TYPE_CHECKING:
    from vs_agent.sink import AgentEventSink
    from vs_agent.skills import SkillSelection

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


def materialize_skills(  # noqa: C901  # tracked: #288
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

    Existing destinations are replaced on every invocation so skill edits are
    picked up across iterations and after candidate checkpoint rollback. Errors
    are logged but never raised — the loop should still make progress even if a
    skill fails to materialize.
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

    for target_rel in (".", *CLI_SKILL_DIRS):
        target_root = workspace / target_rel
        target_root.mkdir(parents=True, exist_ok=True)
        for name, src_skill in discovered.items():
            dest = target_root / name
            try:
                if src_skill.resolve() == dest.resolve():
                    continue
                if dest.exists() or dest.is_symlink():
                    if dest.is_dir() and not dest.is_symlink():
                        shutil.rmtree(dest)
                    else:
                        dest.unlink()
                shutil.copytree(src_skill, dest, symlinks=True, ignore=skip_ignore)
            except OSError as exc:
                message = (
                    f"[skills] failed to materialize {src_skill} -> "
                    f"{dest}: {type(exc).__name__}: {exc}"
                )
                event_sink.agent_output(message + "\n", channel="diagnostic")
                if log_file is not None:
                    log_file.write(message + "\n")
                    log_file.flush()


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
