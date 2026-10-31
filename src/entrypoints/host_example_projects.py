"""This checkout's example catalog and project maker, as the host helper channel and the reads use them.

The example machinery loads on first use, so a process that never asks for an example does not
import it.

Public surface: ``checkout_example_projects``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from entrypoints.launcher import source_checkout_root
from vs_project.api import Git

if TYPE_CHECKING:
    from server.host_helper_requests import ExampleProjects
    from vs_sim.api import Processes


def checkout_example_projects(processes: Processes) -> ExampleProjects:
    """This checkout's example catalog and project maker; loaded on first use.

    ``processes`` runs the Git commands that copy an example out; listing the catalog and
    inspecting a destination run none. The caller keeps the result: each call builds a new one.
    """
    # lint-waiver: LW-271505 [PLC0415]; the helper answers hello without loading the example machinery.
    # > `entrypoints.examples` brings argparse, configparser and the catalog reader; at module
    # > level every channel would load them before `hello`, past the helper's module ceiling
    # > (`tests/e2e/test_host_helper.py`). Moving the machinery into the helper would split the
    # > one implementation `vibesys example` uses, and the same deferral serves `harnesses`.
    from entrypoints.examples import GitExampleMaterializer, HelperExampleProjects  # noqa: PLC0415

    return HelperExampleProjects(
        source_checkout_root(),
        GitExampleMaterializer(Git(processes)),
    )
