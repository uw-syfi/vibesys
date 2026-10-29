"""What a scripted implementer does to a workload's code: a catalog of real source edits.

A recipe is one believable idea about one workload, written down as the text it changes. Applying it
to the workload's source gives code that builds and runs, or, for the recipes meant to fail, code that
does not build or breaks the trusted check. The pure part is here: a recipe turns source text into
source text, or says it does not apply (its anchor is gone, or an earlier edit already made it). The
shell that reads and writes the file is `_script`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable


class EditKind(StrEnum):
    """What a recipe's edit does to the candidate, which decides what the trusted run shows."""

    IMPROVES = "improves"
    NEUTRAL = "neutral"
    REGRESSES = "regresses"
    BREAKS_BUILD = "breaks_build"
    BREAKS_ACCURACY = "breaks_accuracy"

    @property
    def broken(self) -> bool:
        """Whether the trusted build or accuracy check rejects the edited code."""
        return self in {EditKind.BREAKS_BUILD, EditKind.BREAKS_ACCURACY}


_MARKER = re.compile(r"^\s*(?://|#) \[scripted\] (?P<slug>[a-z0-9-]+):", re.MULTILINE)


def applied_slugs(source: str) -> tuple[str, ...]:
    """The recipes whose edits ``source`` carries, in the order they appear."""
    return tuple(match.group("slug") for match in _MARKER.finditer(source))


@dataclass(frozen=True, slots=True)
class Recipe:
    """One idea: how it is pitched to the planner and implementer, and the edit that carries it."""

    slug: str
    kind: EditKind
    title: str
    claim: str
    task: str
    evidence_wanted: str
    summary: str
    """What the implementer says it did, in Markdown."""
    edit: Callable[[str], str | None]
    """The edited source, or ``None`` when the anchor this edit needs is not in it."""
    weight: int = 1
    """How many times the planner's catalog lists it: a likelier idea has more."""

    def apply(self, source: str) -> str | None:
        """``source`` with this recipe's edit, or ``None`` if it applies no longer or never did."""
        if self.slug in applied_slugs(source):
            return None
        edited = self.edit(source)
        return None if edited is None or edited == source else edited


def _replace(old: str, new: str) -> Callable[[str], str | None]:
    """An edit that swaps ``old`` for ``new`` once, or does not apply without ``old``."""

    def edit(source: str) -> str | None:
        return source.replace(old, new, 1) if old in source else None

    return edit


def _replace_all(old: str, new: str, *, extra: str = "") -> Callable[[str], str | None]:
    """An edit that swaps every ``old`` for ``new``, then adds ``extra`` before the first struct."""

    def edit(source: str) -> str | None:
        if old not in source:
            return None
        edited = source.replace(old, new)
        return edited.replace("struct QueueState {", f"{extra}struct QueueState {{", 1)

    return edit


_COPY = """        let mut value = Vec::new();
        if value.try_reserve_exact(data.len()).is_err() {
            return STATUS_INTERNAL_ERROR;
        }
        value.extend_from_slice(data);
        values.push_back(value);
"""
_SINGLE_COPY = """        // [scripted] single-copy: build the stored value in one allocation and one copy.
        values.push_back(data.to_vec());
"""

_SPIN = """// [scripted] spin-before-lock: a short spin usually finds the lock free again, so the thread
// does not park and wake for a critical section this small.
fn lock_spinning<T>(mutex: &Mutex<T>) -> Option<std::sync::MutexGuard<'_, T>> {
    for _ in 0..64 {
        match mutex.try_lock() {
            Ok(guard) => return Some(guard),
            Err(std::sync::TryLockError::WouldBlock) => std::hint::spin_loop(),
            Err(std::sync::TryLockError::Poisoned(_)) => return None,
        }
    }
    mutex.lock().ok()
}

"""
_LOCK = "let Ok(mut values) = self.state.values.lock() else {"
_SPINNING_LOCK = "let Some(mut values) = lock_spinning(&self.state.values) else {"

_FULL = """        if values.len() == self.state.capacity {
            return STATUS_FULL;
        }
"""
_FULL_BACKOFF = """        if values.len() == self.state.capacity {
            // [scripted] backoff-on-full: give a consumer time to drain before reporting full.
            drop(values);
            std::thread::sleep(std::time::Duration::from_micros(200));
            return STATUS_FULL;
        }
"""
_FULL_EARLY = """        if values.len() + 1 >= self.state.capacity {
            // [scripted] early-full-check: report full one slot early, so the length can never
            // reach the capacity.
            return STATUS_FULL;
        }
"""
_LENGTH_CHECK = "        if data.len() > self.state.max_value_size {\n"
_TYPED_LENGTH = (
    "        // [scripted] typed-length: keep the length in the width the C interface uses.\n"
    "        let length: u32 = data.len();\n" + _LENGTH_CHECK
)


def _inline_hot_path(source: str) -> str | None:
    enqueue, dequeue = "    fn try_enqueue(", "    fn try_dequeue("
    if enqueue not in source or dequeue not in source:
        return None
    marked = source.replace(
        enqueue,
        "    // [scripted] inline-hot-path: let the callers inline the two calls the benchmark makes.\n"
        "    #[inline]\n" + enqueue,
        1,
    )
    return marked.replace(dequeue, "    #[inline]\n" + dequeue, 1)


QUEUE_RECIPES = (
    Recipe(
        slug="single-copy",
        kind=EditKind.IMPROVES,
        title="Store each value with one copy",
        claim=(
            "An enqueue allocates, reserves and then copies each value in separate steps; "
            "doing it in one copy shortens the time the lock is held."
        ),
        task=(
            "In `Producer::try_enqueue`, build the stored value with a single `to_vec` instead of "
            "reserving and extending, then keep the build and the check green."
        ),
        evidence_wanted="The check passes and throughput rises over the parent.",
        summary=(
            "Replaced the reserve-then-extend sequence in `try_enqueue` with a single `to_vec`, "
            "so the value is allocated and copied once while the lock is held."
        ),
        edit=_replace(_COPY, _SINGLE_COPY),
        weight=2,
    ),
    Recipe(
        slug="spin-before-lock",
        kind=EditKind.IMPROVES,
        title="Spin briefly before parking on the lock",
        claim=(
            "The critical sections are a few instructions long, so a thread that parks on the "
            "mutex pays more for the wake-up than the wait it avoids."
        ),
        task=(
            "Take the queue's lock with a short bounded spin on `try_lock` before falling back to "
            "`lock`, in both `try_enqueue` and `try_dequeue`."
        ),
        evidence_wanted="The check passes and throughput rises with several producers and consumers.",
        summary=(
            "Added `lock_spinning`, which tries the lock 64 times with a spin hint before it "
            "blocks, and used it in both the enqueue and the dequeue path."
        ),
        edit=_replace_all(_LOCK, _SPINNING_LOCK, extra=_SPIN),
        weight=2,
    ),
    Recipe(
        slug="inline-hot-path",
        kind=EditKind.NEUTRAL,
        title="Inline the two hot calls",
        claim="Call overhead on `try_enqueue` and `try_dequeue` is a visible part of each operation.",
        task="Mark `try_enqueue` and `try_dequeue` `#[inline]` and measure.",
        evidence_wanted="The check passes and throughput does not fall.",
        summary="Marked `try_enqueue` and `try_dequeue` `#[inline]`; no other change.",
        edit=_inline_hot_path,
    ),
    Recipe(
        slug="backoff-on-full",
        kind=EditKind.REGRESSES,
        title="Back off when the queue is full",
        claim=(
            "A producer that finds the queue full retries at once and fights the consumers for the "
            "lock; a short pause lets them drain first."
        ),
        task="When `try_enqueue` finds the queue full, release the lock and pause briefly before it reports full.",
        evidence_wanted="The check passes and throughput rises when producers outrun consumers.",
        summary=(
            "On a full queue `try_enqueue` now drops the lock and sleeps 200 microseconds before it "
            "returns `FULL`."
        ),
        edit=_replace(_FULL, _FULL_BACKOFF),
    ),
    Recipe(
        slug="early-full-check",
        kind=EditKind.BREAKS_ACCURACY,
        title="Shorten the full check",
        claim="The capacity comparison can be made once, ahead of the push, with no loss of safety.",
        task="Test for a full queue ahead of the push so the push itself needs no check.",
        evidence_wanted="The check passes and throughput rises.",
        summary="Moved the capacity comparison ahead of the push so that it never lets the length reach capacity.",
        edit=_replace(_FULL, _FULL_EARLY),
    ),
    Recipe(
        slug="typed-length",
        kind=EditKind.BREAKS_BUILD,
        title="Carry lengths in the interface's width",
        claim="Holding the value length as `u32` avoids a widening on every call.",
        task="Keep the value length in a `u32` local in `try_enqueue`, as the C interface does.",
        evidence_wanted="The build and the check pass and throughput does not fall.",
        summary="Kept the value length in a `u32` local at the top of `try_enqueue`.",
        edit=_replace(_LENGTH_CHECK, _TYPED_LENGTH),
    ),
)
"""The edits for the queue example (`src/lib.rs` of the Rust queue)."""


def catalog(recipes: tuple[Recipe, ...]) -> tuple[Recipe, ...]:
    """The recipes, each listed as many times as its weight, in a fixed order."""
    return tuple(recipe for recipe in recipes for _ in range(recipe.weight))
