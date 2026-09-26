"""Shared hypothesis strategies and builders for the rocprof profiler test suite.

Reused across ``test_rocprof_counters.py``, ``test_rocprof_att.py``,
``test_analyze_rocprof.py``, ``test_torch_profile_analyzer.py``, and
``test_rocprof_kernel_bench.py`` so each property-based regression test builds
its fuzzed CSV rows / kernel names / intervals the same way instead of
hand-rolling generators per module.

Every ``@given`` test in this suite should use one of the ``settings`` objects
below (or an equally small ``max_examples``) -- these are pure-Python
functions, so a handful of examples is enough to pin the invariant, and the
whole added suite is expected to run in well under 20s.
"""

from __future__ import annotations

import csv
import io
import string

from hypothesis import settings
from hypothesis import strategies as st

# Bounded example counts, no wall-clock deadline: CI machines vary in speed
# and a few tests build small CSV files on disk via tmp_path.
FAST = settings(max_examples=20, deadline=None)
FEWER = settings(max_examples=12, deadline=None)

ARCH_NAMES = ("gfx90a", "gfx942", "gfx950")
arch_name = st.sampled_from(ARCH_NAMES)


def case_variant(word: str) -> st.SearchStrategy[str]:
    """Every letter independently upper/lowercased.

    Exercises the case-insensitive column-name fallback both counters.py's
    ``_row_from_mapping`` and analyze_rocprof.py's ``_get`` implement.
    """
    flags = st.lists(st.booleans(), min_size=len(word), max_size=len(word))
    return flags.map(
        lambda fs: "".join(c.upper() if f else c.lower() for c, f in zip(word, fs, strict=True))
    )


def permuted_csv(header: list[str], rows: list[list[object]], order: list[int]) -> str:
    """Render header+rows as CSV text with columns permuted by ``order``."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([header[i] for i in order])
    for row in rows:
        writer.writerow([row[i] for i in order])
    return buf.getvalue()


def column_order(n: int) -> st.SearchStrategy[list[int]]:
    """A random permutation of ``range(n)`` -- fuzzes CSV column order."""
    return st.permutations(range(n))


_IDENT = st.text(alphabet=string.ascii_lowercase + "_", min_size=1, max_size=8)


def wrap_with_namespace_and_template_noise(marker: str) -> st.SearchStrategy[str]:
    """Wrap a known family marker in random namespace depth / template-argument noise.

    Simulates the deeply-nested ``ck::tensor_operation::device::...<T1, T2>``
    style names real AITER/CK/Tensile kernels carry: classification must key
    off the marker substring regardless of how much namespace/template noise
    surrounds it.
    """

    @st.composite
    def _build(draw: st.DrawFn) -> str:
        depth = draw(st.integers(min_value=0, max_value=4))
        prefix_parts = draw(st.lists(_IDENT, min_size=depth, max_size=depth))
        prefix = "::".join([*prefix_parts, marker]) if prefix_parts else marker
        n_templates = draw(st.integers(min_value=0, max_value=3))
        suffix = ""
        for _ in range(n_templates):
            a, b = draw(_IDENT), draw(_IDENT)
            suffix += f"<{a}, {b}>"
        return prefix + suffix

    return _build()


def huge_kernel_name() -> st.SearchStrategy[str]:
    """A 5000+ char, template-soup-shaped kernel name.

    Real PyTorch RNG-fill / Tensile GEMM kernel names observed on hardware
    run from a few hundred to several thousand characters (see counters.py's
    ``_short_kernel_name`` docstring).
    """
    chunk = st.text(alphabet=string.ascii_letters + "_<>:,0123456789 ", min_size=10, max_size=40)
    return st.lists(chunk, min_size=150, max_size=250).map(
        lambda parts: ("kernel_" + "".join(parts)).ljust(5000, "x")
    )


def intervals(max_count: int = 8) -> st.SearchStrategy[list[tuple[int, int]]]:
    """A list of (start, duration) pairs, duration >= 1 -- for busy/idle merge tests."""
    pair = st.tuples(
        st.integers(min_value=0, max_value=100_000),
        st.integers(min_value=1, max_value=5_000),
    )
    return st.lists(pair, min_size=0, max_size=max_count)


def buffer_size_input() -> st.SearchStrategy[object]:
    """Plain ints and unit-suffixed strings ("64MB", "1G") -- the raw CLI buffer-size values."""
    return st.one_of(
        st.integers(min_value=1, max_value=2**31),
        st.integers(min_value=1, max_value=4096).map(lambda n: f"{n}MB"),
        st.integers(min_value=1, max_value=16).map(lambda n: f"{n}G"),
    )


# Starts with a letter -- real Python identifiers do, and this keeps a
# leading-underscore prefix (added below) from ever producing a "__"-prefixed
# name, which ``_looks_like_triton_kernel`` deliberately treats as *not*
# Triton (it is the real ``__amd_rocclr_*`` ROCr-runtime naming shape).
_TRITON_WORD = st.text(alphabet=string.ascii_lowercase, min_size=1, max_size=1).flatmap(
    lambda first: st.text(
        alphabet=string.ascii_lowercase + "_0123456789", min_size=0, max_size=19
    ).map(lambda rest: first + rest)
)


def triton_jit_style_kernel_name() -> st.SearchStrategy[str]:
    """A snake_case Triton-JIT kernel name with no torch.inductor ``triton_*`` prefix.

    Modeled on real vLLM/SGLang/FLA kernels observed on hardware --
    ``fused_recurrent_gated_delta_rule_packed_decode_kernel``,
    ``chunk_gated_delta_rule_fwd_kernel_h_blockdim64``, ``_topk_topp_kernel``,
    ``rotary_kernel`` -- compiled straight from ``@triton.jit`` so the kernel
    keeps the Python function name verbatim. Used to check
    ``_looks_like_triton_kernel``/``_classify_family`` classify these as
    Triton even without a ``triton_`` prefix.
    """

    @st.composite
    def _build(draw: st.DrawFn) -> str:
        words = draw(st.lists(_TRITON_WORD, min_size=1, max_size=4))
        suffix = draw(st.sampled_from(("_kernel", "_kernel_h_blockdim64", "_kernel_o")))
        name = "_".join(words) + suffix
        if draw(st.booleans()):
            name = "_" + name
        return name

    return _build()


def cpp_template_kernel_name() -> st.SearchStrategy[str]:
    """A hand-written HIP/C++ kernel name with template args -- not Triton.

    Modeled on real non-Triton kernels that would otherwise look
    Triton-shaped by naming convention alone, e.g.
    ``wvSplitK_hf_sml_<__hip_bfloat16, 64, 4, 16, 8, 2, 2>(int, int, ...)``.
    Used as a negative example for ``_looks_like_triton_kernel``: template
    angle brackets must rule out the Triton fallback regardless of any
    leading underscore or ``_kernel`` substring in the base name.
    """
    ident = st.text(alphabet=string.ascii_letters + "_", min_size=1, max_size=12)
    arg = st.text(alphabet=string.ascii_letters + string.digits + "_", min_size=1, max_size=8)

    @st.composite
    def _build(draw: st.DrawFn) -> str:
        base = draw(ident)
        args = ", ".join(draw(st.lists(arg, min_size=1, max_size=4)))
        return f"void {base}_kernel_<{args}>(int, int)"

    return _build()
