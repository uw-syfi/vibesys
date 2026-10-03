from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from tests.support import run_test_command


def test_queue_abi_header_supports_cpp_linkage(tmp_path: Path) -> None:
    compiler = shutil.which("c++")
    if compiler is None:
        pytest.skip("a C++ compiler is required to check the queue ABI header")

    project_root = Path(__file__).parents[2]
    include_dir = project_root / "resources" / "evaluators" / "queue" / "include"
    source = tmp_path / "candidate.cpp"
    source.write_text(
        '#include "vibesys_queue_abi.h"\n'
        'extern "C" uint32_t vsq_abi_version() { return VSQ_ABI_VERSION; }\n'
    )
    run_test_command(
        [
            compiler,
            "-std=c++17",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-I",
            str(include_dir),
            "-c",
            str(source),
            "-o",
            str(tmp_path / "candidate.o"),
        ],
        check=True,
    )
