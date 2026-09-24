"""Resolve policy selection from current and legacy run manifests."""

from __future__ import annotations

from vs_project.api import OrchestrationRunManifest, RunManifestRecord


def orchestration_id(manifest: RunManifestRecord) -> str:
    """Return the policy ID recorded under either manifest schema."""
    if isinstance(manifest, OrchestrationRunManifest):
        return manifest.orchestration.id
    return manifest.configuration.outer_loop
