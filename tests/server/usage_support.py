"""Generators and doubles for the usage rollup, summary and scan tests.

The generators draw from the contract: ``vibesys.host.v1.UsageRollupRow`` and its documented
rules (a quarter is epoch seconds over 900, input counts cache reads and writes, a provider is an
agentshim id), plus what the contract allows and the code might not expect: garbled provider and
model strings, cache counts above the input, empty strings, a row with no recorded cost. Nothing
here calls the code under test.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from hypothesis import strategies as st

from server.host_api import RUN_EVENTS_FILE
from server.run_summary_cache import summarize_journal
from server.usage_rollup import QUARTER_SECONDS, FoldedJournal, JournalIdentity
from server.usage_summary import Priced, RunInput
from vs_project.api import Project
from vs_proto.api import Timestamp, UsageCostBasis, UsageSpeed, usage_state

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from server.run_summary import Summarized, SummaryState

PROVIDERS = ("claude_code", "codex", "opencode", "gemini")
MODELS = ("opus-5-5", "Opus-5-5[1m]", "gpt-6.1-sol", "haiku-5-5", "<synthetic>", "mystery-model")
FIRST_QUARTER = 1_790_000_000 // QUARTER_SECONDS
BASES = (
    UsageCostBasis.PRICE_TABLE,
    UsageCostBasis.PROVIDER_REPORTED,
    UsageCostBasis.UNKNOWN_MODEL,
    UsageCostBasis.NOT_REPORTED,
    UsageCostBasis.LEGACY_RECORDED,
    UsageCostBasis.LEGACY_UNPRICED,
)

_COUNT = st.integers(min_value=0, max_value=5_000_000)
_PROVIDER = st.sampled_from(PROVIDERS) | st.just("") | st.text(max_size=12) | st.just("A" * 40)
_MODEL = st.sampled_from(MODELS) | st.just("") | st.text(max_size=20)


@st.composite
def rollup_rows(draw: st.DrawFn, *, quarters: int = 400) -> usage_state.UsageRollupRow:
    """A row anywhere the contract allows, in a span of ``quarters`` quarter hours."""
    row = usage_state.UsageRollupRow(
        quarter=FIRST_QUARTER + draw(st.integers(min_value=0, max_value=quarters)),
        provider=draw(_PROVIDER),
        model=draw(_MODEL),
        speed=draw(st.sampled_from([UsageSpeed.STANDARD, UsageSpeed.FAST, UsageSpeed.ULTRAFAST])),
        role=draw(st.sampled_from(["", "planner", "implementer", "judge"])),
        basis=draw(st.sampled_from(BASES)),
        input=draw(_COUNT),
        cache_read=draw(_COUNT),
        cache_write=draw(_COUNT),
        cache_write_1h=draw(_COUNT),
        output=draw(_COUNT),
        reasoning=draw(_COUNT),
        turns=draw(st.integers(min_value=0, max_value=50)),
    )
    if draw(st.booleans()):
        row.recorded_usd = draw(st.floats(min_value=0, max_value=500, allow_nan=False))
    return row


@st.composite
def rollups(
    draw: st.DrawFn, *, quarters: int = 400, max_rows: int = 12
) -> usage_state.RunUsageRollup:
    """A rollup of a run: rows in any order, repeated keys included."""
    rollup = usage_state.RunUsageRollup(
        version=1,
        journal=usage_state.JournalIdentity(boundary=1, head_sha256="0" * 64, tail_sha256="0" * 64),
        rows=draw(st.lists(rollup_rows(quarters=quarters), max_size=max_rows)),
    )
    for _ in range(draw(st.integers(min_value=0, max_value=3))):
        rollup.new_bests = [
            *rollup.new_bests,
            Timestamp(
                seconds=(FIRST_QUARTER + draw(st.integers(min_value=0, max_value=quarters)))
                * QUARTER_SECONDS
            ),
        ]
    return rollup


_NAME = st.text(min_size=1, max_size=128).filter(
    lambda text: 1 <= len(text.encode()) <= 128
) | st.just("é" * 64)
_PRICE = st.floats(min_value=0, max_value=1_000_000, allow_nan=False)


def _numbered(base: str, index: int) -> str:
    """``base`` with its tail replaced by ``index``, still at most 128 bytes."""
    tag = f"-{index}".encode()
    return (base.encode()[: 128 - len(tag)].decode(errors="ignore").encode() + tag).decode()


@st.composite
def price_books(draw: st.DrawFn, *, largest: bool = False) -> usage_state.UsagePriceBook:
    """A price book as the contract allows: up to 256 overrides and 256 aliases, names to 128 bytes.

    ``largest`` fills every list and makes every name as long as it may be, multi-byte. Not
    necessarily a book a host would accept (its ``Map to`` chains may loop): the reply echoes the
    book, and its size is what is checked.
    """
    count = 256 if largest else draw(st.integers(min_value=0, max_value=256))
    base = "é" * 64 if largest else draw(_NAME)
    rate = usage_state.UsageRate(
        input=draw(_PRICE),
        output=draw(_PRICE),
        cache_read=draw(_PRICE),
        cache_write=draw(_PRICE),
        cache_write_1h=draw(_PRICE),
    )
    return usage_state.UsagePriceBook(
        revision=draw(st.integers(min_value=0, max_value=2**64 - 1)),
        overrides=[
            usage_state.UsageRateOverride(model=_numbered(base, index), rate=rate)
            for index in range(count)
        ],
        aliases=[
            usage_state.UsageAlias(from_=_numbered(base, index), to=_numbered(base, index + 1))
            for index in range(count)
        ],
    )


@st.composite
def run_inputs(draw: st.DrawFn, *, quarters: int = 400, max_runs: int = 6) -> list[RunInput]:
    count = draw(st.integers(min_value=0, max_value=max_runs))
    return [
        RunInput(
            project=draw(st.integers(min_value=0, max_value=3)),
            run_id=f"20261010-1200{index:02d}-aaaaaaaa-dynamic",
            rollup=draw(rollups(quarters=quarters)),
        )
        for index in range(count)
    ]


@st.composite
def edge_lists(draw: st.DrawFn, *, quarters: int = 400) -> list[datetime]:
    """Strictly increasing whole-quarter edges, 2 to 100 of them, anywhere around the rows."""
    count = draw(st.integers(min_value=2, max_value=100))
    marks = draw(
        st.lists(
            st.integers(min_value=-20, max_value=quarters + 20),
            min_size=count,
            max_size=count,
            unique=True,
        )
    )
    return [
        datetime.fromtimestamp((FIRST_QUARTER + mark) * QUARTER_SECONDS, UTC)
        for mark in sorted(marks)
    ]


class TablePricing:
    """A ``Pricing`` over a fixed table: every row of a listed model costs a cent per thousand tokens."""

    def __init__(
        self,
        listed: Sequence[str] = ("opus-5-5", "gpt-6.1-sol"),
        book: usage_state.UsagePriceBook | None = None,
    ) -> None:
        self._listed = frozenset(listed)
        self._book = book or usage_state.UsagePriceBook()

    def price(self, provider: str, row: usage_state.UsageRollupRow) -> Priced:
        del provider
        model = row.model.strip().lower() or "unknown"
        if model not in self._listed:
            return Priced(model=model, source=usage_state.UsageEstimateSource.UNPRICED)
        tokens = row.input + row.output
        cost = tokens * 1e-5
        return Priced(
            model=model,
            source=usage_state.UsageEstimateSource.MODEL,
            estimate_usd=cost,
            input_usd=cost / 2,
            output_usd=cost / 2,
        )

    def resolved(
        self, provider: str, model: str, speed: UsageSpeed
    ) -> tuple[usage_state.UsageRate | None, usage_state.UsageEstimateSource]:
        del provider, speed
        if model not in self._listed:
            return None, usage_state.UsageEstimateSource.UNPRICED
        return (
            usage_state.UsageRate(input=10, output=10, cache_read=1, cache_write=12),
            usage_state.UsageEstimateSource.MODEL,
        )

    @property
    def pricing(self) -> usage_state.UsagePricing:
        return usage_state.UsagePricing(
            status=usage_state.UsagePricingStatus.BUNDLED, known_models=len(self._listed)
        )

    @property
    def book(self) -> usage_state.UsagePriceBook:
        return self._book

    @property
    def prices_status(self) -> usage_state.UsagePricesStatus:
        return usage_state.UsagePricesStatus.OK


@dataclass
class MemoryRollupStore:
    """A run's rollup kept in memory, as strict as the file: it holds what ``to_binary`` holds."""

    stored: bytes | None = None
    writes: int = 0

    def read(self) -> usage_state.RunUsageRollup | None:
        return None if self.stored is None else usage_state.RunUsageRollup.from_binary(self.stored)

    def write(self, rollup: usage_state.RunUsageRollup) -> None:
        self.stored = rollup.to_binary()
        self.writes += 1


@dataclass
class MemoryUsageJournal:
    """A journal held in memory, with the identity the summary cache gives its complete lines.

    ``grow`` appends a line, as the run's writer would; ``state`` is what a fold of the journal
    returns, set by the test.
    """

    text: bytes = b""
    state: SummaryState | None = None
    folds: int = 0
    _bests: tuple[datetime, ...] = field(default=())

    def grow(self, line: bytes) -> None:
        self.text += line

    def identity(self) -> JournalIdentity:
        end = self.text.rfind(b"\n") + 1
        head = self.text[: min(4096, end)]
        tail = self.text[max(0, end - 64 * 1024) : end]
        return JournalIdentity(
            end, hashlib.sha256(head).hexdigest(), hashlib.sha256(tail).hexdigest()
        )

    def cold_bytes(self) -> int:
        return len(self.text)

    def fold(self) -> FoldedJournal:
        assert self.state is not None
        self.folds += 1
        return FoldedJournal(self.state, self._bests, self.identity(), persistable=True)


class Projects:
    """The host's projects for ``collect_runs``: the helper's own summary of the journals."""

    def run_summary(self, project: Path, run_id: str) -> Summarized:
        return summarize_journal(Project.log_directory_for(project, run_id) / RUN_EVENTS_FILE)

    def run_digest(self, project_root: Path, run_id: str) -> None:
        del project_root, run_id


def write_run(project: Path, run_id: str, body: bytes) -> Path:
    journal = Project.log_directory_for(project, run_id) / RUN_EVENTS_FILE
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_bytes(body)
    return journal
