"""Modal deployment of the baseline engine on two H100s.

``modal deploy main.py`` serves the same ``python -m engine.server`` that every
task launches, behind a Modal web endpoint whose readiness URL is ``/health``.
It is a development convenience for CUDA hosts: none of this project's tasks
runs on Modal. This file only supplies the image, two H100s and a persistent
Hugging Face cache, so the first start downloads the BF16 weights (about
70 GB) and later starts read them from the volume.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import modal

APP_NAME = os.environ.get("VIBESYS_MODAL_APP_NAME", "vibesys-qwen36-35b-a3b-baseline")
HF_CACHE = "/root/.cache/huggingface"
APP_DIR = "/root/app"
PORT = 8000

_here = Path(__file__).parent
image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install_from_requirements(str(_here / "requirements.txt"))
    .env({"HF_HOME": HF_CACHE, "PYTHONUNBUFFERED": "1"})
    .add_local_dir(str(_here / "engine"), remote_path=f"{APP_DIR}/engine")
)
hf_cache = modal.Volume.from_name("vibesys-hf-cache", create_if_missing=True)
app = modal.App(APP_NAME)


@app.function(
    image=image, gpu="H100:2", volumes={HF_CACHE: hf_cache}, timeout=3600, scaledown_window=600
)
@modal.concurrent(max_inputs=256)
@modal.web_server(PORT, startup_timeout=1800)
def serve() -> None:
    subprocess.Popen(
        ["python", "-m", "engine.server", "--host", "0.0.0.0", "--port", str(PORT)], cwd=APP_DIR
    )
