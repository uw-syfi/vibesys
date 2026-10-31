"""This checkout's example catalog and project maker, as the host helper channel and the reads use them.

The example machinery (``entrypoints.examples``: argparse, configparser, the catalog reader) loads
when the first request for an example arrives, so a process that never asks for one does not
import it.

Public surface: ``ExampleProjectsOnUse``.
"""

from __future__ import annotations

from functools import cached_property
from typing import TYPE_CHECKING

from entrypoints.launcher import source_checkout_root
from server.lazy_modules import lazy_module
from vs_project.api import Git

if TYPE_CHECKING:
    from pathlib import Path

    from entrypoints import examples
    from server.host_examples import CreatedProject, ExampleDestination, ExampleList
    from server.host_helper_requests import ExampleProjects
    from vs_sim.api import Processes
else:
    examples = lazy_module("entrypoints.examples")


class ExampleProjectsOnUse:
    """The ``ExampleProjects`` of this checkout, which loads the example machinery on first use."""

    def __init__(self, processes: Processes) -> None:
        """Bind the processes the example machinery will run Git with (to copy an example out).

        Listing the catalog and inspecting a destination run none.
        """
        self._processes = processes

    @cached_property
    def _loaded(self) -> ExampleProjects:
        return examples.HelperExampleProjects(
            source_checkout_root(), examples.GitExampleMaterializer(Git(self._processes))
        )

    def list(self, *, with_catalogue: bool = False) -> ExampleList:
        """Every catalogued example with its tasks, and with their catalogue facts if asked."""
        return self._loaded.list(with_catalogue=with_catalogue)

    def create(self, example: str, destination: Path) -> CreatedProject:
        """Copy ``example`` into its own new Git repository at ``destination``."""
        return self._loaded.create(example, destination)

    def destination(self, example: str, destination: Path) -> tuple[ExampleDestination, str | None]:
        """What :meth:`create` would find at ``destination``, and the project there if it is one."""
        return self._loaded.destination(example, destination)

    def copy_out(self, source: Path, destination: Path) -> CreatedProject:
        """Copy ``source``'s tracked files into their own new Git repository at ``destination``."""
        return self._loaded.copy_out(source, destination)
