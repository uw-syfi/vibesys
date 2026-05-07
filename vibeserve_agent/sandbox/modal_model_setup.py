"""Auto-provisioning of Modal Volumes holding HuggingFace model weights.

Keeps a volume-per-model, named ``vibeserve-model-<sanitized-model-id>``.
When a volume is first requested, :func:`ensure_model_volume` spawns a
one-off Modal function that pulls the snapshot from HuggingFace into the
volume (via ``hf_transfer`` for speed).  Subsequent runs detect the
populated volume and return its name immediately.
"""

from __future__ import annotations

import os
import re

import modal

_UPLOAD_APP_NAME = "vibeserve-model-upload"
_VOL_PREFIX = "vibeserve-model-"
# Sentinel file written after a successful snapshot_download — lets us
# distinguish "volume exists but upload was interrupted" from "ready".
_READY_SENTINEL = "/.vibeserve_ready"


def _volume_name_for(model_id: str) -> str:
    """Return a canonical Modal Volume name for a HuggingFace model id."""
    sanitized = re.sub(r"[^a-z0-9]+", "-", model_id.lower()).strip("-")
    return f"{_VOL_PREFIX}{sanitized}"


def _volume_is_ready(volume: modal.Volume) -> bool:
    """Return True if the volume has a ``_READY_SENTINEL`` marker at its root."""
    try:
        entries = volume.listdir("/")
    except Exception:
        return False
    ready_name = _READY_SENTINEL.lstrip("/")
    return any(e.path.lstrip("/") == ready_name for e in entries)


def ensure_model_volume(
    model_id: str,
    revision: str | None = None,
    hf_token: str | None = None,
    local_path: str | None = None,
    *,
    log: callable = print,
) -> str:
    """Ensure a populated Modal Volume exists for *model_id* and return its name.

    Lookup/upload strategy:

    1. If the named volume already has the ready sentinel → no-op.
    2. If *local_path* is a valid directory (resolved snapshot) →
       ``batch_upload`` from the host.  Used when weights are already
       cached locally (gated models without HF_TOKEN, offline runs, …).
    3. Otherwise spawn a Modal function that runs ``snapshot_download``
       on Modal's side — avoids laptop egress and uses ``hf_transfer``
       for fast parallel download.

    Args:
        model_id: HuggingFace model id, e.g. ``"meta-llama/Llama-3.1-8B-Instruct"``.
        revision: Optional HF revision pin (HF-download path only).
        hf_token: Optional HF token for gated models.  If ``None``, the
            helper reads ``HF_TOKEN`` or ``HUGGING_FACE_HUB_TOKEN`` from the
            environment.
        local_path: Optional host path to a resolved model snapshot
            directory.  If provided and valid, preferred over HF download.
        log: Callable used to report progress (defaults to ``print``).

    Returns:
        The name of the Modal Volume that was ensured.  Pass this as
        ``--modal-model-volume``.
    """
    from pathlib import Path

    vol_name = _volume_name_for(model_id)
    volume = modal.Volume.from_name(vol_name, create_if_missing=True)

    if _volume_is_ready(volume):
        log(f"[modal] model volume {vol_name} is ready — skipping upload")
        return vol_name

    if local_path:
        resolved = Path(local_path).resolve()
        if resolved.is_dir() and any(resolved.iterdir()):
            log(
                f"[modal] uploading {model_id} to volume {vol_name} "
                f"from local path {resolved} (~ GB, from your machine)..."
            )
            with volume.batch_upload(force=True) as batch:
                batch.put_directory(str(resolved), "/")
                batch.put_file(
                    __import__("io").BytesIO(b"ok\n"),
                    _READY_SENTINEL,
                )
            log(f"[modal] volume {vol_name} populated and ready")
            return vol_name

    token = hf_token or os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    log(
        f"[modal] populating volume {vol_name} with {model_id}"
        + (f"@{revision}" if revision else "")
        + " (downloading on Modal side)..."
    )

    app = modal.App(_UPLOAD_APP_NAME)
    image = (
        modal.Image.debian_slim()
        .pip_install("huggingface_hub[hf_transfer]>=0.26")
        .env({"HF_HUB_ENABLE_HF_TRANSFER": "1"})
    )
    secrets: list[modal.Secret] = []
    if token:
        secrets.append(modal.Secret.from_dict({"HF_TOKEN": token}))

    @app.function(
        image=image,
        volumes={"/vol": volume},
        timeout=3600,
        secrets=secrets,
        cpu=4.0,
        memory=8192,
        serialized=True,
    )
    def _download(mid: str, rev: str | None, sentinel: str) -> None:
        import os as _os
        from pathlib import Path
        from huggingface_hub import snapshot_download

        snapshot_download(
            mid,
            revision=rev,
            local_dir="/vol",
            token=_os.environ.get("HF_TOKEN"),
        )
        Path("/vol" + sentinel).write_text("ok\n")

    with app.run():
        _download.remote(model_id, revision, _READY_SENTINEL)

    log(f"[modal] volume {vol_name} populated and ready")
    return vol_name
