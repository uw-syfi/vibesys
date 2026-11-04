"""Entry ids name ledger requests by key prefix (#2757)."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from vs_sim.api.testing import examples
from vs_slurm.api import ENTRY_ID_LENGTH, Resolved, Unresolved, entry_id, resolve_entries

_KEYS = st.sets(st.text("0123456789abcdef", min_size=48, max_size=48), min_size=1, max_size=6)


@given(keys=_KEYS, data=st.data())
@settings(max_examples=examples(pr=30, nightly=300), deadline=None)
def test_each_unique_prefix_resolves_to_its_one_key_in_the_order_given(
    keys: set[str], data: st.DataObject
) -> None:
    ordered = tuple(sorted(keys))
    chosen = data.draw(st.lists(st.sampled_from(ordered), unique=True))
    ids = []
    for key in chosen:
        unique = next(
            length
            for length in range(1, len(key) + 1)
            if sum(other.startswith(key[:length]) for other in ordered) == 1
        )
        ids.append(key[: data.draw(st.integers(unique, len(key)))].upper())

    assert resolve_entries(ordered, tuple(ids)) == Resolved(tuple(chosen))


@given(keys=_KEYS, data=st.data())
@settings(max_examples=examples(pr=30, nightly=300), deadline=None)
def test_a_prefix_of_several_keys_or_of_none_resolves_nothing(
    keys: set[str], data: st.DataObject
) -> None:
    ordered = tuple(sorted(keys))
    given_id = data.draw(st.text("0123456789abcdef", max_size=4))
    matches = [key for key in ordered if given_id and key.startswith(given_id)]

    outcome = resolve_entries(ordered, (given_id,))

    if len(matches) == 1:
        assert outcome == Resolved((matches[0],))
    elif matches:
        assert outcome == Unresolved((), (given_id,))
    else:
        assert outcome == Unresolved((given_id,), ())


@given(key=st.text("0123456789abcdef", min_size=48, max_size=48))
def test_an_entry_id_is_a_prefix_of_its_key(key: str) -> None:
    assert key.startswith(entry_id(key))
    assert len(entry_id(key)) == ENTRY_ID_LENGTH
