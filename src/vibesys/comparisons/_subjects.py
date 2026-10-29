"""The comparisons a task declares, resolved for one machine into what pins their numbers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vs_runtime.api.comparisons import ComparisonSubject, context_digest

if TYPE_CHECKING:
    from vibesys.inputs import InputBundle
    from vs_project.api import ComparisonInput
    from vs_sandbox.api.slurm import SlurmOperatorSettings

_DIGEST_MARK = "@sha256:"


def _identity(bundle: InputBundle, spec: ComparisonInput) -> str:
    """The image's digest, or the hash of the Dockerfile's build context (its directory)."""
    if spec.image is not None:
        return spec.image.rsplit(_DIGEST_MARK, 1)[1]
    if spec.dockerfile is None:
        message = f"comparison {spec.name!r} declares no image source"
        raise ValueError(message)
    dockerfile = (bundle.task_root / spec.dockerfile).resolve()
    return "context-" + context_digest(dockerfile.parent)


def _evaluator(bundle: InputBundle) -> str:
    evaluator = bundle.manifest.evaluator
    if evaluator is None:
        return "none"
    if evaluator.source is not None:
        return "task-source"
    return f"{evaluator.name}@{evaluator.version}"


def hardware_of(bundle: InputBundle, settings: SlurmOperatorSettings) -> str:
    """The hardware a reading was taken on: the task's declared accelerator and the job's shape."""
    catalogue = bundle.manifest.catalogue
    declared = (
        f"{catalogue.hardware.accelerator} x{catalogue.hardware.count}"
        if catalogue is not None
        else "undeclared"
    )
    return f"{declared}; sbatch {' '.join(settings.config.sbatch_arguments)}".strip()


def comparison_subjects(
    bundle: InputBundle, settings: SlurmOperatorSettings
) -> tuple[ComparisonSubject, ...]:
    """Every comparison *bundle* declares, resolved for the machine *settings* describes."""
    hardware = hardware_of(bundle, settings)
    return tuple(
        ComparisonSubject(
            spec=spec,
            identity=_identity(bundle, spec),
            evaluator=_evaluator(bundle),
            benchmark=bundle.resolved_benchmark_command,
            hardware=hardware,
        )
        for spec in bundle.manifest.comparison
    )
