"""Domain-specific runtime setup hooks.

Prompt domains describe what agents should know. Runtime domains describe what
the VibeServe environment must prepare before those agents start working.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class RunEnvironmentCapabilities(Protocol):
    materialize_local_model_weights: bool


@dataclass(frozen=True)
class DomainSetupContext:
    """Inputs available to a first-party domain runtime during setup."""

    reference_path: Path
    workspace: Path
    run_environment: RunEnvironmentCapabilities
    project_root: Path
    log: Callable[[str], None]


@dataclass(frozen=True)
class DomainBindMount:
    """A host path that the domain wants exposed inside the runtime."""

    host_path: Path
    container_path: str
    read_only: bool = True


@dataclass(frozen=True)
class DomainEnvironmentPatch:
    """Declarative changes requested by a domain runtime."""

    copy_excludes: frozenset[str] = frozenset()
    bind_mounts: tuple[DomainBindMount, ...] = ()


class DomainRuntime(Protocol):
    def prepare_environment(self, ctx: DomainSetupContext) -> DomainEnvironmentPatch: ...

    def teardown_environment(self, ctx: DomainSetupContext) -> None: ...


class NoopDomainRuntime:
    """Runtime policy for domains with no environment-specific setup."""

    def prepare_environment(self, ctx: DomainSetupContext) -> DomainEnvironmentPatch:
        return DomainEnvironmentPatch()

    def teardown_environment(self, ctx: DomainSetupContext) -> None:
        return None


def _ensure_model_weights(ref_dir: Path, *, project_root: Path, log: Callable[[str], None]) -> None:
    """Ensure model weights exist in ref_dir/model, downloading if needed."""
    model_path = ref_dir / "model"

    if model_path.is_symlink() and not model_path.exists():
        model_path.unlink()

    if model_path.exists():
        return

    meta_path = ref_dir / "meta.json"
    if not meta_path.exists():
        raise FileNotFoundError(
            f"Model weights not found at {model_path} and no meta.json to download from. "
            f"Either create a model/ directory/symlink or add a meta.json with model_id."
        )

    meta = json.loads(meta_path.read_text())
    model_id = meta.get("model_id")
    if not model_id:
        raise ValueError(f"meta.json at {meta_path} missing required 'model_id' field")

    revision = meta.get("revision")
    cache_dir = project_root / ".hf_cache"
    log(f"[model] Weights not found at {model_path}. Downloading {model_id} to {cache_dir}...")
    from huggingface_hub import snapshot_download

    downloaded_path = snapshot_download(model_id, revision=revision, cache_dir=str(cache_dir))
    model_path.symlink_to(downloaded_path)
    log(f"[model] Created symlink {model_path} -> {downloaded_path}")


class LLMServingDomainRuntime:
    """Runtime policy for the built-in llm-serving domain."""

    _MODEL_ARTIFACT_NAMES = frozenset({"model", "draft_model"})

    def prepare_environment(self, ctx: DomainSetupContext) -> DomainEnvironmentPatch:
        ref_path = ctx.reference_path
        if not ref_path.is_dir():
            return DomainEnvironmentPatch()

        model_path = ref_path / "model"
        meta_path = ref_path / "meta.json"
        if ctx.run_environment.materialize_local_model_weights or (
            not meta_path.exists() and not model_path.exists()
        ):
            _ensure_model_weights(ref_path, project_root=ctx.project_root, log=ctx.log)

        bind_mounts: list[DomainBindMount] = []
        if model_path.is_dir() or model_path.is_symlink():
            bind_mounts.append(DomainBindMount(model_path, "/model", True))

        draft_model_path = ref_path / "draft_model"
        if draft_model_path.is_dir() or draft_model_path.is_symlink():
            bind_mounts.append(DomainBindMount(draft_model_path, "/draft_model", True))

        return DomainEnvironmentPatch(
            copy_excludes=self._MODEL_ARTIFACT_NAMES,
            bind_mounts=tuple(bind_mounts),
        )

    def teardown_environment(self, ctx: DomainSetupContext) -> None:
        return None


def runtime_for_domain_name(domain_name: str) -> DomainRuntime:
    if domain_name == "llm-serving":
        return LLMServingDomainRuntime()
    return NoopDomainRuntime()
