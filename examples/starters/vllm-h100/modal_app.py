from __future__ import annotations

import modal

app = modal.App("vibesys-vllm-h100-candidate")

image = (
    modal.Image.from_registry("nvidia/cuda:12.8.1-devel-ubuntu22.04", add_python="3.12")
    .apt_install("git")
    .pip_install("vllm==0.10.0", "transformers==4.53.2", "httpx>=0.27", "jsonschema>=4.0")
)


@app.function(
    image=image,
    gpu="H100",
    timeout=7200,
    cpu=8.0,
    memory=65536,
)
def smoke() -> str:
    return "vllm-h100 starter ready"
