"""This checkout's example catalog and project maker, as the host helper channel and the reads use them.

The example machinery (``entrypoints.examples``: argparse, configparser, the catalog reader) is
heavy, so this module is a lazy module of its callers: a process that never asks for an example
does not import it.

Public surface: ``checkout_example_projects``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from entrypoints.examples import GitExampleMaterializer, HelperExampleProjects
from entrypoints.launcher import source_checkout_root
from vs_project.api import Git

if TYPE_CHECKING:
    from server.host_helper_requests import ExampleProjects
    from vs_sim.api import Processes


def checkout_example_projects(processes: Processes) -> ExampleProjects:
    """This checkout's example catalog and project maker.

    ``processes`` runs the Git commands that copy an example out; listing the catalog and
    inspecting a destination run none. The caller keeps the result: each call builds a new one.
    """
    return HelperExampleProjects(source_checkout_root(), GitExampleMaterializer(Git(processes)))
