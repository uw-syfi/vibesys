"""MAP-Elites archive for the openevolve loop.

A thin layer over :class:`vibe_serve.loops.evolve.population.Population`
that bins individuals by behavioral features and exposes cell-uniform
parent sampling.

## Feature space

Two features by default — both *behavioral*, not fitness-derived, so
selection on cells doesn't smuggle the perf metric back in:

- ``code_size_bucket``: lines in ``main.py`` quantized into
  ``[0-300, 300-600, 600-1000, 1000+]`` (4 bins).
- ``technique_bucket``: count of distinct optimization techniques the
  current ``main.py`` *names* (regex over a small lexicon: cuda graph,
  flash attention, paged attention, eagle/spec decode, torch.compile,
  continuous batching, fp8, awq/gptq). Bucketed into ``[0, 1, 2, 3+]``
  (4 bins). Names are a coarse proxy for "what techniques the code is
  attempting" — false positives (e.g. mentions in a comment) are
  acceptable noise; we only need a discrete, behavior-shaped axis.

Together they form a 4×4 = 16-cell grid. Cells are keyed by the
``(bucket0, bucket1)`` tuple. The grid is sparse — most cells are
empty until the loop has produced enough offspring to fill them.

## Selection

``sample_cell()`` picks a non-empty cell uniformly at random; the
elite within (highest ``perf_metric``) is the parent. Inspirations
are pulled by sampling additional cells (without replacement when
possible) to surface diverse strategies, falling back to within-cell
random peers when the archive is sparse.
"""

from __future__ import annotations

import random
import re
from pathlib import Path

from vibe_serve.loops.evolve.population import Individual, Population


# ---------------------------------------------------------------------------
# Feature extraction
# ---------------------------------------------------------------------------

_CODE_SIZE_THRESHOLDS = (300, 600, 1000)  # → 4 buckets: <300, <600, <1000, ≥1000
_TECHNIQUE_THRESHOLDS = (1, 2, 3)         # → 4 buckets: 0, 1, 2, ≥3
_DEFAULT_GRID_SHAPE = (4, 4)

_TECHNIQUE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("cuda_graph", re.compile(r"cuda[_\s\-]?graph", re.IGNORECASE)),
    ("flash_attention", re.compile(r"flash[_\s\-]?attn|flashattention|flashinfer", re.IGNORECASE)),
    ("paged_attention", re.compile(r"paged[_\s\-]?attention|page[_\s\-]?table", re.IGNORECASE)),
    ("speculative_decoding", re.compile(r"\beagle[0-9]?\b|spec(?:ulative)?[_\s\-]?decod|\bdraft[_\s\-]?model\b", re.IGNORECASE)),
    ("torch_compile", re.compile(r"torch\.compile|@compile\b", re.IGNORECASE)),
    ("continuous_batching", re.compile(r"continuous[_\s\-]?batch", re.IGNORECASE)),
    ("low_precision", re.compile(r"\bfp8\b|\bint8\b|\bawq\b|\bgptq\b|\bbnb\b", re.IGNORECASE)),
    ("xgrammar", re.compile(r"xgrammar|grammar[_\s\-]?mask|jump[_\s\-]?forward", re.IGNORECASE)),
)


def _bucket(value: int, thresholds: tuple[int, ...]) -> int:
    """Return the bucket index for *value* given ascending *thresholds*.

    ``len(thresholds) + 1`` total buckets. ``thresholds = (a, b, c)``
    yields buckets ``[<a, <b, <c, ≥c]``.
    """
    for i, t in enumerate(thresholds):
        if value < t:
            return i
    return len(thresholds)


def _count_main_py_lines(workspace: Path) -> int:
    """Return the number of newline-terminated lines in ``main.py``.

    Returns 0 if the file does not exist (cold start). The implementer
    is expected to write ``main.py`` at the workspace root; we don't
    walk subdirectories because the rule is always-flat.
    """
    main_py = workspace / "main.py"
    if not main_py.is_file():
        return 0
    try:
        return main_py.read_text(errors="replace").count("\n")
    except OSError:
        return 0


def _count_techniques(workspace: Path) -> int:
    main_py = workspace / "main.py"
    if not main_py.is_file():
        return 0
    try:
        text = main_py.read_text(errors="replace")
    except OSError:
        return 0
    hits = 0
    for _, pat in _TECHNIQUE_PATTERNS:
        if pat.search(text):
            hits += 1
    return hits


def compute_features(workspace: Path) -> dict[str, int]:
    """Return the MAP-Elites feature dict for the current ``workspace``.

    Reads ``workspace/main.py`` only — no git operations, no profile
    invocation. Safe to call repeatedly between rounds.
    """
    return {
        "code_size_bucket": _bucket(_count_main_py_lines(workspace), _CODE_SIZE_THRESHOLDS),
        "technique_bucket": _bucket(_count_techniques(workspace), _TECHNIQUE_THRESHOLDS),
    }


# ---------------------------------------------------------------------------
# Archive
# ---------------------------------------------------------------------------


_FEATURE_ORDER: tuple[str, ...] = ("code_size_bucket", "technique_bucket")


def _cell_key(features: dict[str, int]) -> tuple[int, ...]:
    """Stable tuple key for *features*, padding missing axes with 0."""
    return tuple(int(features.get(name, 0)) for name in _FEATURE_ORDER)


class MapElitesArchive:
    """Behavioral-feature-binned view over a :class:`Population`.

    Wraps the existing population so persistence (population.json), the
    ``Individual`` schema, and downstream observability stay shared with
    the evolve loop. The archive itself is derived state — rebuilt from
    the population on every read.

    Failed individuals (``passed=False``) are kept in the population
    but excluded from the archive: a failed offspring has no perf_metric
    to seed its cell elite, and re-mutating from a failure would just
    repeat the dead end the judge already rejected.
    """

    def __init__(self, population: Population) -> None:
        self._population = population

    @property
    def population(self) -> Population:
        return self._population

    # -- elites --------------------------------------------------------------

    def cells(self) -> dict[tuple[int, ...], Individual]:
        """Return ``{cell_key: elite_individual}``.

        The elite of a cell is the passed individual with the highest
        ``perf_metric``; ties broken by latest id. Cells with no passed
        member are absent. Individuals missing both ``features`` and
        ``perf_metric`` are skipped (cold-start records).
        """
        bins: dict[tuple[int, ...], Individual] = {}
        for ind in self._population.passed:
            if ind.perf_metric is None:
                continue
            key = _cell_key(ind.features)
            current = bins.get(key)
            if current is None:
                bins[key] = ind
                continue
            cur_perf = current.perf_metric or float("-inf")
            if ind.perf_metric > cur_perf or (
                ind.perf_metric == cur_perf and ind.id > current.id
            ):
                bins[key] = ind
        return bins

    def __len__(self) -> int:
        return len(self.cells())

    def coverage(self) -> float:
        """Fraction of grid cells filled (0.0 .. 1.0)."""
        total = 1
        for _ in _FEATURE_ORDER:
            total *= _DEFAULT_GRID_SHAPE[0]  # all axes share the same shape today
        return len(self) / total if total else 0.0

    # -- selection -----------------------------------------------------------

    def sample_cell_elite(self, *, rng: random.Random) -> Individual | None:
        """Return the elite of a uniformly-sampled non-empty cell.

        Returns ``None`` when the archive is empty (cold start) — the
        caller treats this as "ask the mutator to write the first
        working server from the reference", same as the evolve loop's
        cold-start branch.
        """
        elites = self.cells()
        if not elites:
            return None
        cell_key = rng.choice(list(elites.keys()))
        return elites[cell_key]

    def sample_inspirations(
        self,
        *,
        parent_id: int | None,
        k: int,
        rng: random.Random,
    ) -> list[Individual]:
        """Pick *k* peers, prioritizing diverse cells.

        Strategy:

        1. Sample up to *k* additional non-empty cells (different from
           the parent's cell), take their elites.
        2. If still under *k*, top up with random passed individuals
           (excluding the parent and anyone already chosen).

        This gives the mutator a small set of *behaviorally distinct*
        examples to learn from — the explicit point of MAP-Elites.
        """
        if k <= 0:
            return []
        elites = self.cells()
        parent_cell: tuple[int, ...] | None = None
        if parent_id is not None:
            parent = self._population.get(parent_id)
            if parent is not None:
                parent_cell = _cell_key(parent.features)

        other_cells = [c for c in elites if c != parent_cell]
        rng.shuffle(other_cells)
        picks = [elites[c] for c in other_cells[:k] if elites[c].id != parent_id]

        if len(picks) < k:
            picked_ids = {p.id for p in picks}
            if parent_id is not None:
                picked_ids.add(parent_id)
            spare = [
                ind for ind in self._population.passed
                if ind.id not in picked_ids and ind.perf_metric is not None
            ]
            rng.shuffle(spare)
            picks.extend(spare[: k - len(picks)])
        return picks
