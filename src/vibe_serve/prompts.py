"""Jinja2 prompt rendering.

Two concepts:

- **Template** — a full prompt the LLM sees as one document
  (e.g. ``orchestrate/templates/implementer/system.j2``). Has structure:
  headers, task description, constraints. Lives in a per-mode
  directory.
- **Fragment** — a small reusable snippet meant to be composed *into*
  a template, not rendered standalone. Lives at
  ``vibe_serve/templates/_backend/<backend>/<name>.j2``. The
  ``_backend/`` prefix marks "fragment directory, not a place to find
  full templates".

:class:`ComputeBackendFragment` is the Python contract for backend fragments:
its :attr:`~ComputeBackendFragment.NAMES` class attribute is the canonical
list of fragment names, and concrete subclasses
(:class:`CudaComputeBackendFragment`, :class:`MetalComputeBackendFragment`) anchor
each backend in the ``_FRAGMENT_IMPLS`` registry. Adding a fragment
name requires updating ``NAMES`` and creating a ``<name>.j2`` file
under every backend dir (an empty file is a deliberate skip).

:class:`Prompt` validates the backend's fragment files exist at
construction time and auto-injects every fragment as a kwarg keyed by
filename stem on every ``render(...)`` call. Templates can therefore
reference ``{{ device_dtype }}`` regardless of which backend the run
targets.

See ``vibe_serve/templates/_backend/README.md`` for the
fragment-filename convention contributors should follow.
"""

from abc import ABC
from pathlib import Path
from typing import ClassVar

from jinja2 import Environment, FileSystemLoader

from vibe_serve.constants import ComputeBackend

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    keep_trailing_newline=True,
    trim_blocks=True,
    lstrip_blocks=True,
)

# Cache of Jinja2 environments keyed by template directory path
_env_cache: dict[str, Environment] = {str(_TEMPLATES_DIR): _env}


def _build_env(template_dir: Path | str | None = None) -> Environment:
    """Return a Jinja2 Environment for the given template directory.

    Per-mode template directories also fall back to the shared
    ``vibe_serve/templates/`` root, so ``{% include
    "_backend/<name>/foo.j2" %}`` from a per-mode template (and
    fragment lookups via :class:`ComputeBackendFragment`) resolve from the
    shared root.
    """
    if template_dir is None:
        return _env
    key = str(template_dir)
    if key not in _env_cache:
        search_paths = [key]
        if key != str(_TEMPLATES_DIR):
            search_paths.append(str(_TEMPLATES_DIR))
        _env_cache[key] = Environment(
            loader=FileSystemLoader(search_paths),
            keep_trailing_newline=True,
            trim_blocks=True,
            lstrip_blocks=True,
        )
    return _env_cache[key]


def render_template(
    name: str,
    *,
    template_dir: Path | str | None = None,
    **kwargs: object,
) -> str:
    """Render a Jinja2 template (no fragment auto-injection).

    Thin wrapper used by call sites that don't need backend-aware
    fragment composition (e.g. curriculum mode). New backend-aware
    code should use :class:`Prompt` instead.
    """
    env = _build_env(template_dir)
    return env.get_template(name).render(**kwargs)


def render_string(source: str, **kwargs: object) -> str:
    """Render a Jinja2 template from an in-memory string.

    Used by call sites that hold the template text directly rather than a
    file on disk (e.g. a domain pack's role section parsed out of a single
    Markdown file). Shares the root environment's settings so ``{% if %}``
    trimming matches file-based templates.
    """
    return _env.from_string(source).render(**kwargs)


class ComputeBackendFragment(ABC):
    """Provides backend-specific Jinja fragments under
    ``vibe_serve/templates/_backend/<backend>/``.

    Subclasses must set ``backend = ComputeBackend.<X>``. The default
    rendering reads ``<backend>/<name>.j2`` from the shared templates
    root; override :meth:`render` to compute fragments dynamically.

    Adding a fragment name requires:

    1. Adding the name to :attr:`NAMES`.
    2. Creating ``<name>.j2`` under every concrete subclass's backend
       directory. An empty file is a deliberate skip (renders to empty
       string); short placeholder prose is a soft skip that gives the
       LLM context.

    :meth:`validate` checks the on-disk contract — one ``.j2`` file
    per name in ``NAMES``.
    """

    NAMES: ClassVar[frozenset[str]] = frozenset({
        "device_dtype",
        "judge_device_correctness",
        "profiling_workflow",
    })
    backend: ClassVar[ComputeBackend]  # set by subclasses

    def __init__(self, env: Environment) -> None:
        self._env = env

    def render(self, name: str) -> str:
        """Render a single fragment by name.

        Strips trailing newlines from the rendered output: fragments
        are inline substitutions (`{{ device_dtype }}` mid-line), so
        the parent template owns the surrounding whitespace. Without
        this, ``keep_trailing_newline=True`` on the env would inject
        an extra blank line at every substitution site.
        """
        if name not in self.NAMES:
            raise ValueError(
                f"Unknown fragment {name!r}; valid: {sorted(self.NAMES)}"
            )
        return self._env.get_template(
            f"_backend/{self.backend.value}/{name}.j2"
        ).render().rstrip("\n")

    def render_all(self) -> dict[str, str]:
        """Render every fragment in :attr:`NAMES` keyed by name."""
        return {name: self.render(name) for name in self.NAMES}

    @classmethod
    def validate(cls) -> None:
        """Verify a ``.j2`` file exists for every fragment in
        :attr:`NAMES`. Raises ``ValueError`` listing missing files.
        """
        backend_dir = _TEMPLATES_DIR / "_backend" / cls.backend.value
        missing = [
            n for n in cls.NAMES if not (backend_dir / f"{n}.j2").is_file()
        ]
        if missing:
            raise ValueError(
                f"{cls.__name__}: missing fragment files under {backend_dir}: "
                f"{', '.join(f'{n}.j2' for n in sorted(missing))}. "
                f"Use an empty file for a deliberate skip."
            )


class CudaComputeBackendFragment(ComputeBackendFragment):
    """Fragments for the CUDA backend (NVIDIA GPUs)."""
    backend = ComputeBackend.CUDA


class MetalComputeBackendFragment(ComputeBackendFragment):
    """Fragments for the Metal backend (Apple Silicon, MPS)."""
    backend = ComputeBackend.METAL


class TrainiumComputeBackendFragment(ComputeBackendFragment):
    """Fragments for the Trainium backend (AWS NeuronCores)."""
    backend = ComputeBackend.TRAINIUM


_FRAGMENT_IMPLS: dict[ComputeBackend, type[ComputeBackendFragment]] = {
    ComputeBackend.CUDA: CudaComputeBackendFragment,
    ComputeBackend.METAL: MetalComputeBackendFragment,
    ComputeBackend.TRAINIUM: TrainiumComputeBackendFragment,
}


def get_backend_fragment(backend: ComputeBackend, env: Environment) -> ComputeBackendFragment:
    """Construct the :class:`ComputeBackendFragment` impl for the given backend."""
    if backend not in _FRAGMENT_IMPLS:
        raise ValueError(
            f"No ComputeBackendFragment registered for {backend!r}. "
            f"Registered: {sorted(_FRAGMENT_IMPLS.keys(), key=lambda b: b.value)}"
        )
    return _FRAGMENT_IMPLS[backend](env)


class Prompt:
    """Render templates from a per-mode directory, with backend fragments
    auto-injected as kwargs.

    Construction validates the bound backend's fragment files exist
    (via :meth:`ComputeBackendFragment.validate`); a missing file fails fast
    with a clear error rather than silently rendering an empty kwarg.

    Each call to :meth:`render` re-renders every fragment (no caching)
    and passes them as kwargs keyed by filename stem. Explicit kwargs
    passed to :meth:`render` override auto-injected ones.

    Parameters
    ----------
    template_dir:
        Per-mode directory the renderer searches first (e.g.
        ``orchestrate/templates/``). Falls back to the shared
        ``vibe_serve/templates/`` root, where backend fragments
        live.
    backend:
        Hardware backend the run targets. Selects the
        :class:`ComputeBackendFragment` impl whose fragments get
        auto-injected.
    """

    def __init__(self, template_dir: Path | str, backend: ComputeBackend) -> None:
        self._env = _build_env(template_dir)
        self._fragments = get_backend_fragment(backend, self._env)
        type(self._fragments).validate()

    def render(self, name: str, **kwargs: object) -> str:
        """Render a full template.

        ComputeBackend fragments are auto-injected as kwargs keyed by
        filename stem; explicit kwargs override.
        """
        auto = self._fragments.render_all()
        return self._env.get_template(name).render(**(auto | kwargs))

    def fragment(self, name: str) -> str:
        """Render a single backend fragment by name (escape hatch).

        Useful for tests and for cases that want a single fragment
        without rendering a parent template.
        """
        return self._fragments.render(name)
