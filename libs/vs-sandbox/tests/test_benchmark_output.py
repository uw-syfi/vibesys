"""Which benchmark result paths a trusted gate accepts, as properties of the path text."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from vs_sandbox.api.slurm import BenchmarkOutputKind, classify_benchmark_output

_TOKEN = st.text(alphabet="abcdef0123456789", min_size=1, max_size=32)


@given(token=st.text(alphabet="abcdefABCDEF0123456789_-", min_size=1, max_size=40))
def test_a_workspace_result_is_one_file_name_in_the_working_directory(token: str) -> None:
    path = f".vibesys-benchmark-{token}.json"
    assert classify_benchmark_output(path) is BenchmarkOutputKind.WORKSPACE


@given(token=_TOKEN)
def test_a_framework_result_is_the_fixed_tmp_path_with_a_hex_nonce(token: str) -> None:
    path = f"/tmp/vibesys-framework-benchmark-{token}.json"  # noqa: S108  # lint-waiver: LW-954370 [S108]; the accepted path shape under test.
    assert classify_benchmark_output(path) is BenchmarkOutputKind.FRAMEWORK


@given(text=st.text(max_size=80))
def test_no_accepted_path_can_leave_its_directory(text: str) -> None:
    """Whatever the text, an accepted path has no separator beyond the fixed /tmp one."""
    kind = classify_benchmark_output(text)
    if kind is BenchmarkOutputKind.WORKSPACE:
        assert "/" not in text
        assert ".." not in text.replace("...", "")
    if kind is BenchmarkOutputKind.FRAMEWORK:
        assert text.count("/") == 2
        assert ".." not in text
    if kind is not None:
        assert "\n" not in text


@given(prefix=st.text(max_size=5), suffix=st.text(max_size=5), token=_TOKEN)
def test_surrounding_text_never_passes(prefix: str, suffix: str, token: str) -> None:
    path = f"/tmp/vibesys-framework-benchmark-{token}.json"  # noqa: S108  # lint-waiver: LW-954371 [S108]; the accepted path shape under test.
    padded = f"{prefix}{path}{suffix}"
    assert (classify_benchmark_output(padded) is not None) == (not prefix and not suffix)
