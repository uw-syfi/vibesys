"""Hermetic tests for the disallowed-engine-code scan (no GPU, no server).
uv run pytest examples/model-serving/qwen3.5-397b-a17b-mi300a-bespoke/accuracy_checker/test_engine_scan.py -q --no-cov -p no:tach
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import engine_scan


def _scan(files: dict[str, str]) -> list[str]:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for name, text in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        return engine_scan.scan(root)


class EngineScanTest(unittest.TestCase):
    def test_clean_workspace(self) -> None:
        self.assertEqual(_scan({"server.py": "import torch\nimport triton\nimport aiter\n"}), [])

    def test_flags_imports(self) -> None:
        for source in (
            "import vllm\n",
            "from sglang.srt.layers import x\n",
            "import tensorrt_llm.runtime as r\n",
            "import importlib\nm = importlib.import_module('sgl_kernel.ops')\n",
        ):
            with self.subTest(source=source):
                self.assertTrue(_scan({"server.py": source}))

    def test_flags_pip_installs_but_not_comments(self) -> None:
        self.assertTrue(_scan({"setup.sh": "pip install vllm\n"}))
        self.assertTrue(_scan({"requirements.txt": "uv pip install sglang-router\n"}))
        self.assertEqual(_scan({"setup.sh": "# pip install vllm is not allowed\n"}), [])

    def test_flags_vendored_directory(self) -> None:
        self.assertTrue(_scan({"third_party/sglang/scheduler.py": "x = 1\n"}))

    def test_harness_and_state_directories_are_skipped(self) -> None:
        files = {
            "accuracy_checker/x.py": "import vllm\n",
            "benchmark/y.py": "import sglang\n",
            "reference/z.py": "import vllm\n",
            ".venv/lib/a.py": "import vllm\n",
            "server.py": "import torch\n",
        }
        self.assertEqual(_scan(files), [])


if __name__ == "__main__":
    unittest.main()
