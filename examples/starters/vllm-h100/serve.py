from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Run local editable vLLM as an OpenAI server.")
    parser.add_argument("--model-path", default=os.environ.get("MODEL_PATH", "/model"))
    parser.add_argument("--served-model-name", default=os.environ.get("SERVED_MODEL_NAME", "llama"))
    parser.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    parser.add_argument("--port", default=os.environ.get("PORT", "8000"))
    parser.add_argument("--dtype", default=os.environ.get("DTYPE", "bfloat16"))
    parser.add_argument(
        "--gpu-memory-utilization", default=os.environ.get("GPU_MEMORY_UTILIZATION", "0.90")
    )
    parser.add_argument("--max-model-len", default=os.environ.get("MAX_MODEL_LEN", "8192"))
    parser.add_argument(
        "--tensor-parallel-size", default=os.environ.get("TENSOR_PARALLEL_SIZE", "1")
    )
    parser.add_argument(
        "--vllm-arg",
        action="append",
        default=[],
        help="Extra raw argument forwarded to vLLM. Repeat for each argv element.",
    )
    args = parser.parse_args()

    workspace = Path(__file__).resolve().parent
    local_vllm = workspace / "vllm"
    if not (local_vllm / "vllm").is_dir():
        raise SystemExit("Expected a local vLLM checkout at ./vllm from workspace.sources")

    env = os.environ.copy()
    env.setdefault("VLLM_USE_V1", "1")
    env["PYTHONPATH"] = f"{local_vllm}{os.pathsep}{env.get('PYTHONPATH', '')}"

    cmd = [
        sys.executable,
        "-m",
        "vllm.entrypoints.openai.api_server",
        "--model",
        args.model_path,
        "--served-model-name",
        args.served_model_name,
        "--host",
        args.host,
        "--port",
        args.port,
        "--dtype",
        args.dtype,
        "--tensor-parallel-size",
        args.tensor_parallel_size,
        "--gpu-memory-utilization",
        args.gpu_memory_utilization,
        "--max-model-len",
        args.max_model_len,
        "--disable-log-requests",
        *args.vllm_arg,
    ]
    raise SystemExit(subprocess.call(cmd, env=env))


if __name__ == "__main__":
    main()
