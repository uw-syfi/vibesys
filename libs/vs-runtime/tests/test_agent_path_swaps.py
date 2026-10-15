"""An agent that swaps a workspace directory for a symlink never leads a host read or write out.

The agent can replace any directory in its workspace while the host works on a path below
it. Each test arms a swap that fires at the n-th filesystem operation the host performs
(seen through a Python audit hook, so the swap lands at an exact step, never by timing)
and sweeps n over every operation, including the one between a resolve-then-open host's
check and its use.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from vs_runtime.api import CommandResult
from vs_runtime.api.infrastructure import (
    LocalValidator,
    ModelRequestError,
    create_model_request_reconciler,
)
from vs_runtime.api.testing import (
    FakeCommands,
    FakeModelVolumeProvisioner,
    FakeWorkspace,
    MemoryValidationPasses,
)
from vs_sim.api.testing import examples

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_runtime.api.infrastructure import FrameworkValidationResult, ValidationRecipe

# The audit events of the calls a host makes to open, create or rename a path.
_FILESYSTEM_EVENTS = frozenset(
    {"open", "os.mkdir", "os.rename", "os.scandir", "os.listdir", "tempfile.mkstemp"}
)
_MAX_POSITIONS = 24
_MODEL_MANIFEST = ".vibesys/models.json"


@dataclass
class _Swap:
    """Run ``action`` once, at the ``position``-th filesystem event after it is armed."""

    action: Callable[[], None] | None = None
    remaining: int = 0
    fired: bool = False
    seen: int = 0
    _running: bool = field(default=False, repr=False)

    def arm(self, action: Callable[[], None], position: int) -> None:
        self.action, self.remaining, self.fired, self.seen = action, position, False, 0

    def disarm(self) -> None:
        self.action = None

    def __call__(self, event: str, args: tuple[object, ...]) -> None:
        del args
        if self.action is None or self._running or event not in _FILESYSTEM_EVENTS:
            return
        self.seen += 1
        self.remaining -= 1
        if self.remaining:
            return
        action, self.action = self.action, None
        self._running = True
        try:
            action()
        finally:
            self._running = False
        self.fired = True


# An audit hook cannot be removed, so one inert-until-armed hook serves the whole process.
_SWAP = _Swap()
sys.addaudithook(_SWAP)


def _swap_for_symlink(directory: Path, target: Path) -> Callable[[], None]:
    """Move ``directory`` aside inside its parent and put a symlink to ``target`` there."""

    def swap() -> None:
        directory.rename(directory.with_name(f"{directory.name}-moved"))
        directory.symlink_to(target, target_is_directory=True)

    return swap


def _files_below(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
    }


@dataclass
class _ArmAfterRecipes:
    """Arm the swap once the last recipe finished, so it lands in the report write."""

    action: Callable[[], None]
    position: int

    def started(self, recipe: ValidationRecipe) -> None:
        del recipe

    def finished(self, result: FrameworkValidationResult) -> None:
        del result
        _SWAP.arm(self.action, self.position)


def _report_write_with_swap(root: Path, position: int) -> tuple[bool, dict[str, bytes]]:
    """Validate with the report's directory swapped at ``position``; fired, outside files."""
    workspace = root / "workspace"
    outside = root / "outside"
    outside.mkdir()
    (outside / "authorized_keys").write_bytes(b"host secret\n")
    reports = workspace / "reports"
    reports.mkdir(parents=True)
    (workspace / "queue.py").write_text("VALUE = 1\n")
    (workspace / "recipes.json").write_text(
        json.dumps(
            {
                "version": 1,
                "recipes": [
                    {
                        "name": "focused",
                        "command": "pytest -q",
                        "input_paths": ["queue.py"],
                        "timeout_seconds": 30,
                        "purpose": "exercise the candidate",
                    }
                ],
            }
        )
    )
    commands = FakeCommands()
    commands.script(CommandResult(output="passed\n", exit_code=0))
    events = _ArmAfterRecipes(_swap_for_symlink(reports, outside), position)

    async def validate() -> None:
        await LocalValidator(commands, MemoryValidationPasses()).run(
            FakeWorkspace(path=workspace),
            recipe_artifact="recipes.json",
            report_location="reports/authorized_keys",
            events=events,
        )

    try:
        asyncio.run(validate())
    except ValueError:
        pass  # Refusing to write through the symlink is a correct outcome.
    finally:
        _SWAP.disarm()
    return _SWAP.fired, _files_below(outside)


def test_a_report_directory_swapped_for_a_symlink_mid_write_is_not_written_through(
    tmp_path: Path,
) -> None:
    fired, outside = _report_write_with_swap(tmp_path, position=1)

    assert fired
    assert outside == {"authorized_keys": b"host secret\n"}


@pytest.mark.parametrize("position", range(1, _MAX_POSITIONS))
def test_no_swap_position_in_the_report_write_leads_it_out_of_the_workspace(
    position: int,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        _, outside = _report_write_with_swap(Path(directory), position)
    assert outside == {"authorized_keys": b"host secret\n"}


def _manifest_read_with_swap(root: Path, position: int) -> tuple[bool, tuple[str, ...]]:
    """Reconcile with the manifest's directory swapped at ``position``; fired, models requested."""
    workspace = root / "workspace"
    manifest = workspace / _MODEL_MANIFEST
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps([{"id": "org/agent"}]))
    outside = root / "outside"
    outside.mkdir()
    (outside / "models.json").write_text(json.dumps([{"id": "org/host-secret"}]))
    provisioner = FakeModelVolumeProvisioner()
    reconciler = create_model_request_reconciler(provisioner=provisioner, environment={})
    _SWAP.arm(_swap_for_symlink(manifest.parent, outside), position)
    try:
        reconciler.reconcile(workspace, log=lambda _line: None)
    except ModelRequestError:
        pass  # Refusing to read through the symlink is a correct outcome.
    finally:
        _SWAP.disarm()
    return _SWAP.fired, tuple(request.model_id for request in provisioner.requests)


def test_a_manifest_directory_swapped_for_a_symlink_mid_read_is_not_read_through(
    tmp_path: Path,
) -> None:
    # The resolve-then-open reader's only filesystem event is its open, after the check.
    fired, requested = _manifest_read_with_swap(tmp_path, position=1)

    assert fired
    assert "org/host-secret" not in requested


@settings(max_examples=examples(pr=8, nightly=48), deadline=None)
@given(position=st.integers(min_value=1, max_value=_MAX_POSITIONS))
def test_no_swap_position_in_the_manifest_read_leads_it_out_of_the_workspace(
    position: int,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        _, requested = _manifest_read_with_swap(Path(directory), position)
    assert "org/host-secret" not in requested
