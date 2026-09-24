"""Synthesize an input bundle from standalone CLI flags.

External users who install VibeSys as a package do not have the repository
``examples/`` bundles on disk. Rather than require them to hand-assemble the
``OBJECTIVE.md`` + ``vibesys.input.toml`` bundle shape, they can pass the same
information as separate flags (objective, domain, evaluator commands, optional
reference/evaluator directories). This module materializes those flags into a
conventional bundle directory that :func:`vibesys.evaluators.input_manifest.load_input_bundle`
then loads unchanged, so every downstream consumer keeps seeing a real
``InputBundle`` rooted at a real directory.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import ValidationError

from vibesys.evaluators.input_manifest import MANIFEST_NAME, InputManifest

if TYPE_CHECKING:
    from pathlib import Path

    from vibesys.constants import DomainName

#: Bundle-relative directory name used to stage trusted evaluator source
#: supplied by standalone flags. It lives inside the synthesized bundle so the
#: bundle is self-contained; ``load_input_bundle(...)`` permits sources that
#: resolve here.
EVALUATOR_SRC_DIRNAME = "_evaluator_src"

#: Top-level entries the synthesizer owns. An ``--evaluator-dir`` whose contents
#: are copied into the bundle root must not collide with any of these.
_RESERVED_ROOT_ENTRIES = frozenset(
    {
        "OBJECTIVE.md",
        MANIFEST_NAME,
        "reference",
        EVALUATOR_SRC_DIRNAME,
    }
)


class InputSynthesisError(ValueError):
    """Raised when standalone input flags cannot be synthesized into a bundle."""

    @classmethod
    def source_missing(cls, label: str, path: Path) -> InputSynthesisError:
        """Describe a source directory that does not exist."""
        return cls(f"{label} path does not exist: {path}")

    @classmethod
    def source_not_directory(cls, label: str, path: Path) -> InputSynthesisError:
        """Describe a source path that is not a directory."""
        return cls(f"{label} path is not a directory: {path}")

    @classmethod
    def incomplete_benchmark_result(cls) -> InputSynthesisError:
        """Describe benchmark result flags missing one of their paired values."""
        return cls(
            "benchmark result requires both --input-benchmark-metric and "
            "--input-benchmark-result-arg."
        )

    @classmethod
    def reserved_entry_collision(cls, name: str) -> InputSynthesisError:
        """Describe evaluator contents that collide with a bundle-owned entry."""
        return cls(f"--input-evaluator-dir entry collides with a reserved bundle name: {name}")

    @classmethod
    def invalid_manifest(cls, error: Exception) -> InputSynthesisError:
        """Describe an internally generated manifest that fails validation."""
        return cls(f"Invalid synthesized manifest: {error}")

    @classmethod
    def destination_exists(cls, path: Path) -> InputSynthesisError:
        """Describe a synthesized bundle destination that already exists."""
        return cls(f"synthesized bundle directory already exists: {path}")


@dataclass(frozen=True)
class SynthesizedInputSpec:
    """Resolved standalone-input flags describing a bundle to synthesize.

    Directory fields are absolute source paths on the operator's machine; their
    contents are copied into the synthesized bundle so it is self-contained.
    """

    objective: str
    domain: DomainName
    accuracy_command: tuple[str, ...]
    benchmark_command: tuple[str, ...]
    accuracy_timeout_seconds: int | None = None
    benchmark_timeout_seconds: int | None = None
    benchmark_metric: str | None = None
    benchmark_result_arg: str | None = None
    reference_dir: Path | None = None
    evaluator_dir: Path | None = None
    evaluator_source_dir: Path | None = None


def _toml_string(value: str) -> str:
    """Render ``value`` as a TOML basic string with the escapes TOML requires."""
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


def _toml_string_array(values: tuple[str, ...]) -> str:
    return "[" + ", ".join(_toml_string(value) for value in values) + "]"


def _require_source_dir(path: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        raise InputSynthesisError.source_missing(label, path)
    if not resolved.is_dir():
        raise InputSynthesisError.source_not_directory(label, path)
    return resolved


def _build_manifest_dict(spec: SynthesizedInputSpec) -> dict[str, object]:
    """Assemble the manifest payload, mirroring the emitted TOML structure."""
    accuracy: dict[str, object] = {"command": list(spec.accuracy_command)}
    if spec.accuracy_timeout_seconds is not None:
        accuracy["timeout_seconds"] = spec.accuracy_timeout_seconds

    benchmark: dict[str, object] = {"command": list(spec.benchmark_command)}
    if spec.benchmark_timeout_seconds is not None:
        benchmark["timeout_seconds"] = spec.benchmark_timeout_seconds
    if spec.benchmark_metric is not None or spec.benchmark_result_arg is not None:
        if spec.benchmark_metric is None or spec.benchmark_result_arg is None:
            raise InputSynthesisError.incomplete_benchmark_result()
        benchmark["result"] = {
            "json_argument": spec.benchmark_result_arg,
            "metric": spec.benchmark_metric,
        }

    manifest: dict[str, object] = {
        "version": 1,
        "agent": {"domain": str(spec.domain)},
        "accuracy": accuracy,
        "benchmark": benchmark,
    }
    if spec.evaluator_source_dir is not None:
        manifest["evaluator"] = {"source": EVALUATOR_SRC_DIRNAME}
    return manifest


def _render_manifest_toml(spec: SynthesizedInputSpec) -> str:
    """Render the fixed manifest shape for ``spec`` as TOML text.

    This is a purpose-built emitter for exactly the tables the synthesizer
    generates, not a general TOML writer. The output round-trips through
    :func:`vibesys.evaluators.input_manifest.load_input_bundle`, which re-validates it. The
    same shape is validated eagerly via :func:`_build_manifest_dict`.
    """
    lines: list[str] = [
        "version = 1",
        "",
        "[agent]",
        f"domain = {_toml_string(str(spec.domain))}",
        "",
        "[accuracy]",
        f"command = {_toml_string_array(spec.accuracy_command)}",
    ]
    if spec.accuracy_timeout_seconds is not None:
        lines.append(f"timeout_seconds = {spec.accuracy_timeout_seconds}")
    lines.append("")

    lines.append("[benchmark]")
    lines.append(f"command = {_toml_string_array(spec.benchmark_command)}")
    if spec.benchmark_timeout_seconds is not None:
        lines.append(f"timeout_seconds = {spec.benchmark_timeout_seconds}")
    lines.append("")
    if spec.benchmark_metric is not None and spec.benchmark_result_arg is not None:
        lines.append("[benchmark.result]")
        lines.append(f"json_argument = {_toml_string(spec.benchmark_result_arg)}")
        lines.append(f"metric = {_toml_string(spec.benchmark_metric)}")
        lines.append("")

    if spec.evaluator_source_dir is not None:
        lines.append("[evaluator]")
        lines.append(f"source = {_toml_string(EVALUATOR_SRC_DIRNAME)}")
        lines.append("")

    return "\n".join(lines).rstrip("\n") + "\n"


def _copy_tree_into(source: Path, destination: Path) -> None:
    shutil.copytree(source, destination, symlinks=True)


def _copy_evaluator_dir_contents(evaluator_dir: Path, bundle_root: Path) -> None:
    """Copy each top-level entry of ``evaluator_dir`` into the bundle root."""
    for child in sorted(evaluator_dir.iterdir()):
        if child.name in _RESERVED_ROOT_ENTRIES:
            raise InputSynthesisError.reserved_entry_collision(child.name)
        target = bundle_root / child.name
        if child.is_dir():
            _copy_tree_into(child, target)
        else:
            shutil.copy2(child, target)


def synthesize_input_bundle(spec: SynthesizedInputSpec, destination: Path) -> Path:
    """Materialize ``spec`` into a bundle directory at ``destination``.

    Returns the created bundle root. The caller loads it with
    ``load_input_bundle(root)``.
    """
    manifest = _build_manifest_dict(spec)
    try:
        InputManifest.model_validate(manifest)
    except ValidationError as exc:  # pragma: no cover - defensive, flags validated upstream
        raise InputSynthesisError.invalid_manifest(exc) from exc

    reference_dir = (
        _require_source_dir(spec.reference_dir, "--input-reference")
        if spec.reference_dir is not None
        else None
    )
    evaluator_dir = (
        _require_source_dir(spec.evaluator_dir, "--input-evaluator-dir")
        if spec.evaluator_dir is not None
        else None
    )
    evaluator_source_dir = (
        _require_source_dir(spec.evaluator_source_dir, "--input-evaluator-source")
        if spec.evaluator_source_dir is not None
        else None
    )
    root = destination.expanduser().resolve()
    if root.exists():
        raise InputSynthesisError.destination_exists(root)
    root.mkdir(parents=True)

    # Evaluator-dir contents go in first so a later reserved-name write always
    # wins and a collision is reported rather than silently overwritten.
    if evaluator_dir is not None:
        _copy_evaluator_dir_contents(evaluator_dir, root)

    objective_text = spec.objective if spec.objective.endswith("\n") else spec.objective + "\n"
    (root / "OBJECTIVE.md").write_text(objective_text)
    (root / MANIFEST_NAME).write_text(_render_manifest_toml(spec))

    if reference_dir is not None:
        _copy_tree_into(reference_dir, root / "reference")
    if evaluator_source_dir is not None:
        _copy_tree_into(evaluator_source_dir, root / EVALUATOR_SRC_DIRNAME)

    return root
