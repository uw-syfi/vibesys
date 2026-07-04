"""Domain packs — pluggable, per-problem-space prompt context.

A *domain* tells the agent loop what kind of system it is building and what
"good" means there: the background knowledge the implementer must read, and the
correctness/performance/integrity gates the judge must enforce. It is selected
with ``--domain`` and authored as a **single Markdown file** whose ``##`` role
sections are injected into the neutral base prompts:

    _domain/<name>.md
    ├── (free-form prose / description — ignored by the loop)
    ├── ## implementer    ← injected as {{ domain_implementer }}
    ├── ## judge          ← injected as {{ domain_judge }}
    └── ## single_agent   ← injected as {{ domain_single_agent }}

The section heading *is* the address: a line that is exactly ``## <role>`` (for a
role in :data:`DOMAIN_ROLES`) starts that role's section, which runs until the
next role heading. The section body is normal Markdown — it may use its own
``##`` sub-headings (those never match a role name) and may use ``{% if %}``
Jinja to branch on the run's context. Any prose before the first role heading is
human documentation and is not injected.

A missing role section injects nothing. ``single_agent`` is special: if the file
has no ``## single_agent`` section, it is *derived* by concatenating the
``implementer`` and ``judge`` sections, so authors don't hand-maintain a third
copy.

``--domain`` accepts a **built-in name** (a ``<name>.md`` under
``loops/agent/templates/_domain/``) or a **path** to a user's own ``.md`` file
anywhere on disk, so users can author their own without touching vibeserve. See
``loops/agent/templates/_domain/README.md`` for the authoring guide.
"""

from __future__ import annotations

from pathlib import Path

from vibe_serve.prompts import render_string

DEFAULT_DOMAIN = "llm-serving"

# The roles a domain pack can contribute to. Each maps to a ``## <role>`` section
# in the domain file and a ``{{ domain_<role> }}`` injection point in the
# corresponding base prompt.
DOMAIN_ROLES: tuple[str, ...] = ("implementer", "judge", "single_agent", "orchestrator")

_BUILTIN_DOMAINS_DIR = Path(__file__).resolve().parent / "templates" / "_domain"


def builtin_domains() -> list[str]:
    """Names of the built-in domain packs (``<name>.md`` files under ``_domain/``)."""
    if not _BUILTIN_DOMAINS_DIR.is_dir():
        return []
    return sorted(p.stem for p in _BUILTIN_DOMAINS_DIR.glob("*.md") if p.name != "README.md")


def resolve_domain(spec: str) -> Path:
    """Resolve a ``--domain`` value to a domain-pack Markdown file.

    ``spec`` is either a path to a ``.md`` file (used as-is) or the name of a
    built-in pack (``_domain/<spec>.md``). Raises ``ValueError`` with the list of
    built-ins if neither resolves.
    """
    candidate = Path(spec).expanduser()
    if candidate.is_file():
        return candidate.resolve()

    builtin = _BUILTIN_DOMAINS_DIR / f"{spec}.md"
    if builtin.is_file():
        return builtin

    raise ValueError(
        f"Unknown domain {spec!r}. Pass a built-in name "
        f"({', '.join(builtin_domains())}) or a path to a domain .md file."
    )


def _role_heading(line: str) -> str | None:
    """Return the role name if ``line`` is exactly a ``## <role>`` heading.

    Only headings whose text matches a name in :data:`DOMAIN_ROLES` delimit a
    section, so a body's own ``## Required: …`` sub-headings are left intact.
    """
    stripped = line.strip()
    for role in DOMAIN_ROLES:
        if stripped == f"## {role}":
            return role
    return None


def _load_sections(domain_file: Path) -> dict[str, str]:
    """Parse a domain file into ``{role: raw_section_text}``.

    Lines before the first role heading (description prose) are ignored.
    """
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in domain_file.read_text().splitlines():
        heading = _role_heading(line)
        if heading is not None:
            current = heading
            sections.setdefault(current, [])
            continue
        if current is not None:
            sections[current].append(line)
    return {role: "\n".join(lines).strip("\n") for role, lines in sections.items()}


def render_domain_section(domain_file: Path, role: str, **context: object) -> str:
    """Render a domain file's ``## <role>`` section, or ``""`` if absent/empty.

    The section is rendered through Jinja with ``context`` — the same uniform
    variable set for every role (``modality``, ``interface``, ``reference_path``,
    ``bench_path``, ``accuracy_checker_path``, ``runtime_notes``; built by
    ``_domain_render_context`` in ``loop.py``) so authors can branch on the run
    from any section.
    ``single_agent`` falls back to ``implementer`` + ``judge`` when the
    file has no explicit ``## single_agent`` section. Leading and trailing blank
    lines are stripped — the base template owns the spacing around the
    ``{{ domain_<role> }}`` injection point.
    """
    sections = _load_sections(domain_file)
    raw = sections.get(role)
    if raw is None and role == "single_agent":
        raw = "\n\n".join(
            text for text in (sections.get("implementer"), sections.get("judge")) if text
        )
    if not raw:
        return ""
    return render_string(raw, **context).strip("\n")
