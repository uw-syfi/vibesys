"""Verify the engine-free image: engines gone, build dependencies intact.
Stdlib only. Run at build time and inside the running container:
    python3 /opt/verify_image.py
Exit code 0 means the image is good.
"""

from __future__ import annotations

import importlib
import sys

ENGINES = ("sglang", "vllm", "tensorrt_llm")
KEPT = (
    "torch",
    "triton",
    "aiter",
    "transformers",
    "safetensors",
    "aiohttp",
    "fastapi",
    "uvicorn",
    "numpy",
)


def _importable(name: str) -> tuple[bool, str]:
    try:
        importlib.import_module(name)
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    return True, ""


def main() -> int:
    errors: list[str] = []
    for name in ENGINES:
        ok, _ = _importable(name)
        if ok:
            errors.append(f"engine still importable: {name}")
    for name in KEPT:
        ok, detail = _importable(name)
        if not ok:
            errors.append(f"kept package failed to import: {name} ({detail})")
    torch = sys.modules.get("torch")
    if torch is not None and getattr(getattr(torch, "version", None), "hip", None) is None:
        errors.append("torch.version.hip is None: not a ROCm build")
    for line in errors:
        print(f"FAIL: {line}", file=sys.stderr)
    if not errors:
        print("OK: engines absent, kept packages import, torch is a ROCm build")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
