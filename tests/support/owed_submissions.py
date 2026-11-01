"""A durable owed-submission record over a real Project state directory."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vs_project.api import Project
from vs_runtime.api.core import KeyedDocuments, OwedSubmissions
from vs_sim.api import OsThreads

if TYPE_CHECKING:
    from pathlib import Path

    from vs_project.api import StateNamespace


def owed_namespace(directory: Path) -> StateNamespace:
    """The state namespace below ``directory`` that holds the owed record."""
    directory.mkdir(parents=True, exist_ok=True)
    return Project.open(directory).state.state_store_namespace("owed")


def owed_in(directory: Path) -> OwedSubmissions:
    """The owed record stored below ``directory``; the same directory gives the same record."""
    return OwedSubmissions(KeyedDocuments(owed_namespace(directory), threads=OsThreads()))
