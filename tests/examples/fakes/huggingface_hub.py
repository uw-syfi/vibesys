"""Environment-configured Fake for the benchmark's Hugging Face download boundary."""

from __future__ import annotations

import json
import os
from pathlib import Path


def hf_hub_download(*, repo_id: str, filename: str, revision: str) -> str:
    """Record an exact-file lookup and return the configured cached artifact."""
    capture = Path(os.environ["HF_FAKE_CAPTURE"])
    capture.write_text(
        json.dumps({"repo_id": repo_id, "filename": filename, "revision": revision}),
        encoding="utf-8",
    )
    local_path = Path(os.environ["HF_FAKE_LOCAL_PATH"])
    if not local_path.is_file():
        raise FileNotFoundError(local_path)
    return str(local_path)
