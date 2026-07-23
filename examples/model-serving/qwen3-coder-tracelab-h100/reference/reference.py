"""Reference loader for the Qwen3-Coder TraceLab serving target.

VibeSys materializes the model named in ``meta.json`` under this reference
directory. This script is intentionally small: correctness for this service
target is checked through the running OpenAI-compatible endpoint rather than by
loading both a candidate and a local reference model in-process.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Print this bundle's reference model metadata.")
    parser.add_argument("--meta", type=Path, default=Path(__file__).with_name("meta.json"))
    args = parser.parse_args()
    print(json.dumps(json.loads(args.meta.read_text()), indent=2))


if __name__ == "__main__":
    main()
