"""A comparison's reading is keyed by what changes its number, and cached where it was measured."""

from __future__ import annotations

import tempfile
from pathlib import Path

from hypothesis import given
from hypothesis import strategies as st

from vs_project.api import ComparisonInput
from vs_runtime.api.comparisons import (
    MAX_REASON_CHARS,
    ComparisonMeasured,
    ComparisonNotMeasured,
    ComparisonSample,
    ComparisonStore,
    ComparisonSubject,
    bounded_reason,
)

_IMAGE = "docker://rocm/vllm@sha256:" + "a" * 64
_WORDS = st.text(alphabet="abcdefghij", min_size=1, max_size=8)
_FLOATS = st.floats(min_value=0.001, max_value=1e6, allow_nan=False, allow_infinity=False)


def _spec(**changes: object) -> ComparisonInput:
    table: dict[str, object] = {
        "name": "vllm",
        "label": "vLLM",
        "image": _IMAGE,
        "serve": ["vllm", "--port", "${PORT}"],
        **changes,
    }
    return ComparisonInput.model_validate(table)


def _subject(
    spec: ComparisonInput, identity: str = "a" * 64, evaluator: str = "ev@1", hardware: str = "gpu"
) -> ComparisonSubject:
    return ComparisonSubject(
        spec=spec, identity=identity, evaluator=evaluator, benchmark=("measure",), hardware=hardware
    )


@given(name=_WORDS, flag=_WORDS, key=_WORDS, value=_WORDS, hardware=_WORDS)
def test_the_key_covers_what_changes_the_number_and_nothing_else(
    name: str, flag: str, key: str, value: str, hardware: str
) -> None:
    base = _subject(_spec(name=name))

    assert base.key == _subject(_spec(name=name)).key
    assert base.key == _subject(_spec(name=name), evaluator="ev@2").key
    # Presentation and operation do not change what is measured.
    assert (
        base.key
        == _subject(
            _spec(name=name, label="other", ready_timeout_seconds=999, visible_to_agents=True)
        ).key
    )
    changed = [
        _subject(_spec(name=name + "x")),
        _subject(_spec(name=name), identity="b" * 64),
        _subject(_spec(name=name), hardware=hardware + "x"),
        _subject(_spec(name=name, serve=["vllm", flag, "${PORT}"])),
        _subject(_spec(name=name, env={key.upper(): value})),
        _subject(_spec(name=name, benchmark_args=[flag])),
    ]
    assert all(other.key != base.key for other in changed)


@given(values=st.lists(_FLOATS, min_size=1, max_size=6))
def test_a_measured_reading_states_its_mean_and_spread(values: list[float]) -> None:
    reading = ComparisonMeasured(
        metric="throughput", samples=tuple(ComparisonSample(value=v) for v in values)
    )

    assert min(values) - 1e-9 <= reading.value <= max(values) + 1e-9
    assert (reading.spread is None) == (len(values) < 2)
    if reading.spread is not None:
        assert reading.spread >= 0


@given(values=st.lists(_FLOATS, min_size=1, max_size=4), at=_FLOATS)
def test_a_stored_reading_comes_back_and_each_put_is_a_new_generation(
    values: list[float], at: float
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        store = ComparisonStore(Path(directory))
        subject = _subject(_spec())
        reading = ComparisonMeasured(
            metric="throughput", samples=tuple(ComparisonSample(value=v) for v in values)
        )

        assert store.get(subject) is None
        assert store.lookup(subject) == ComparisonNotMeasured()
        first = store.put(subject, reading, measured_at=at)
        second = store.put(subject, reading, measured_at=at)

        assert store.get(subject) == second
        assert store.lookup(subject) == reading
        assert (first.generation, second.generation) == (0, 1)
        assert store.has_measured("vllm")
        assert not store.has_measured("sglang")


def test_an_unreadable_or_foreign_file_is_a_miss(tmp_path: Path) -> None:
    store = ComparisonStore(tmp_path)
    subject = _subject(_spec())
    reading = ComparisonMeasured(metric="m", samples=(ComparisonSample(value=1.0),))
    store.put(subject, reading, measured_at=1.0)

    (tmp_path / f"{subject.key}.json").write_text("not json")
    assert store.get(subject) is None
    assert not store.has_measured("vllm")

    other = _subject(_spec(name="sglang"))
    store.put(other, reading, measured_at=1.0)
    (tmp_path / f"{subject.key}.json").write_text((tmp_path / f"{other.key}.json").read_text())
    assert store.get(subject) is None


@given(text=st.text(max_size=2000))
def test_a_reason_is_one_bounded_non_empty_line(text: str) -> None:
    reason = bounded_reason(text)

    assert reason
    assert "\n" not in reason
    assert len(reason) <= MAX_REASON_CHARS


@given(concluded=st.lists(st.booleans(), min_size=1, max_size=8))
def test_an_attempt_resumes_until_it_ends_and_then_the_next_one_is_new(
    concluded: list[bool],
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        store = ComparisonStore(Path(directory))
        subject = _subject(_spec())
        numbers = []
        for ends in concluded:
            number = store.begin_attempt(subject)
            numbers.append(number)
            if ends:
                store.conclude_attempt(subject, number)

        expected = 0
        for index, number in enumerate(numbers):
            assert number == expected
            if concluded[index]:
                expected += 1
