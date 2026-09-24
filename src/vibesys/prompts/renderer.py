"""Jinja2 prompt rendering.

Two concepts:

- **Template** — a full prompt the LLM sees as one document
  (e.g. ``loops/issue_queue/implementer/system.j2``). Has structure:
  headers, task description, constraints. Lives in a per-mode
  directory.
- **Fragment** — a small reusable snippet meant to be composed *into*
  a template, not rendered standalone. Lives at
  ``vibesys/prompts/backend/<backend>/<name>.j2``. The
  ``backend/`` directory marks "fragment directory, not a place to find
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

See ``vibesys/prompts/backend/README.md`` for the
fragment-filename convention contributors should follow.

Rendering itself (Jinja environment construction, strict-undefined
enforcement, and the fragment-family contract) is owned by ``vs_prompts``
(``libs/vs-prompts``); this module binds that generic machinery to
``PROMPTS_DIR`` and to :class:`~vibesys.constants.ComputeBackend`.
"""

from abc import ABC
from pathlib import Path
from typing import ClassVar

from vibesys.constants import ComputeBackend
from vs_prompts.api import FragmentFamily, TemplateRenderer

PROMPTS_DIR = Path(__file__).resolve().parent
_BACKEND_FRAGMENTS_ROOT = PROMPTS_DIR / "backend"

_renderer = TemplateRenderer(PROMPTS_DIR)

# Cache of TemplateRenderers keyed by template directory path
_env_cache: dict[str, TemplateRenderer] = {str(PROMPTS_DIR): _renderer}


def _build_env(template_dir: Path | str | None = None) -> TemplateRenderer:
    """Return a ``TemplateRenderer`` for the given template directory.

    Per-loop prompt directories also fall back to the shared
    ``vibesys/prompts/`` root, so fragment lookups via
    :class:`ComputeBackendFragment` resolve from package-owned prompt assets.
    """
    if template_dir is None:
        return _renderer
    key = str(template_dir)
    if key not in _env_cache:
        _env_cache[key] = (
            _renderer if key == str(PROMPTS_DIR) else _renderer.child(Path(template_dir))
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
    fragment composition. New backend-aware code should use
    :class:`Prompt` instead.
    """
    renderer = _build_env(template_dir)
    return renderer.render_template(name, **kwargs)


def render_string(source: str, **kwargs: object) -> str:
    """Render a Jinja2 template from an in-memory string.

    Used by call sites that hold the template text directly rather than a
    Jinja template file. Shares the root environment's settings so ``{% if %}``
    trimming matches file-based templates.
    """
    return _renderer.render_string(source, **kwargs)


class ComputeBackendFragment(ABC):
    """Provides backend-specific Jinja fragments under
    ``vibesys/prompts/backend/<backend>/``.

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
    """  # noqa: D205  # tracked: #288

    NAMES: ClassVar[frozenset[str]] = frozenset(
        {
            "device_dtype",
            "judge_device_correctness",
            "profiling_workflow",
        }
    )
    backend: ClassVar[ComputeBackend]  # set by subclasses

    def __init__(self, renderer: TemplateRenderer) -> None:  # noqa: D107  # tracked: #288
        self._renderer = renderer
        self._family = FragmentFamily(root=_BACKEND_FRAGMENTS_ROOT, names=self.NAMES)

    def render(self, name: str) -> str:
        """Render a single fragment by name.

        Strips trailing newlines from the rendered output: fragments
        are inline substitutions (`{{ device_dtype }}` mid-line), so
        the parent template owns the surrounding whitespace.
        """
        return self._family.render(self.backend.value, name, self._renderer)

    def render_all(self) -> dict[str, str]:
        """Render every fragment in :attr:`NAMES` keyed by name."""
        return self._family.render_all(self.backend.value, self._renderer)

    @classmethod
    def validate(cls) -> None:
        """Verify a ``.j2`` file exists for every fragment in
        :attr:`NAMES`. Raises ``ValueError`` listing missing files.
        """  # noqa: D205  # tracked: #288
        FragmentFamily(root=_BACKEND_FRAGMENTS_ROOT, names=cls.NAMES).validate([cls.backend.value])


class CudaComputeBackendFragment(ComputeBackendFragment):
    """Fragments for the CUDA backend (NVIDIA GPUs)."""

    backend = ComputeBackend.CUDA


class MetalComputeBackendFragment(ComputeBackendFragment):
    """Fragments for the Metal backend (Apple Silicon, MPS)."""

    backend = ComputeBackend.METAL


class TrainiumComputeBackendFragment(ComputeBackendFragment):
    """Fragments for the Trainium backend (AWS NeuronCores)."""

    backend = ComputeBackend.TRAINIUM


class RocmComputeBackendFragment(ComputeBackendFragment):
    """Fragments for the ROCm backend (AMD Instinct GPUs)."""

    backend = ComputeBackend.ROCM


class CpuComputeBackendFragment(ComputeBackendFragment):
    """Fragments for the CPU backend (no GPU — CPU-bound targets)."""

    backend = ComputeBackend.CPU


_FRAGMENT_IMPLS: dict[ComputeBackend, type[ComputeBackendFragment]] = {
    ComputeBackend.CUDA: CudaComputeBackendFragment,
    ComputeBackend.METAL: MetalComputeBackendFragment,
    ComputeBackend.TRAINIUM: TrainiumComputeBackendFragment,
    ComputeBackend.ROCM: RocmComputeBackendFragment,
    ComputeBackend.CPU: CpuComputeBackendFragment,
}


def get_backend_fragment(backend: ComputeBackend, env: TemplateRenderer) -> ComputeBackendFragment:
    """Construct the :class:`ComputeBackendFragment` impl for the given backend."""
    if backend not in _FRAGMENT_IMPLS:
        raise ValueError(  # noqa: TRY003  # tracked: #288
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
        Per-loop directory the renderer searches first (e.g.
        ``prompts/loops/issue_queue/``). Falls back to the shared
        ``vibesys/prompts/`` root, where backend fragments
        live.
    backend:
        Hardware backend the run targets. Selects the
        :class:`ComputeBackendFragment` impl whose fragments get
        auto-injected.
    """  # noqa: D205  # tracked: #288

    def __init__(self, template_dir: Path | str, backend: ComputeBackend) -> None:  # noqa: D107  # tracked: #288
        self._renderer = _build_env(template_dir)
        self._fragments = get_backend_fragment(backend, self._renderer)
        type(self._fragments).validate()

    def render(self, name: str, **kwargs: object) -> str:
        """Render a full template.

        ComputeBackend fragments are auto-injected as kwargs keyed by
        filename stem; explicit kwargs override.
        """
        auto = self._fragments.render_all()
        return self._renderer.render_template(name, **(auto | kwargs))

    def fragment(self, name: str) -> str:
        """Render a single backend fragment by name (escape hatch).

        Useful for tests and for cases that want a single fragment
        without rendering a parent template.
        """
        return self._fragments.render(name)
