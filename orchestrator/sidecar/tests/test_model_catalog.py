"""The catalog says which models each harness runs, from agentshim, and the process prints it."""

from __future__ import annotations

import subprocess
import sys

from google.protobuf.json_format import Parse

from vs_sidecar.driver import PROVIDERS, model_catalog
from vs_sidecar.gen.vibesys.orchestrator.v1 import orchestrator_pb2 as orch


def test_every_harness_the_sidecar_drives_lists_models_with_its_default_first() -> None:
    catalog = model_catalog()
    assert [entry.harness for entry in catalog.harnesses] == list(PROVIDERS)
    assert all(len(entry.models) > 0 for entry in catalog.harnesses)
    assert all(len(set(entry.models)) == len(entry.models) for entry in catalog.harnesses)


def test_the_catalog_command_prints_the_catalog_and_starts_no_session() -> None:
    done = subprocess.run(  # the interpreter running this test, with a fixed argument
        [sys.executable, "-m", "vs_sidecar", "catalog"],
        capture_output=True,
        check=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    assert Parse(done.stdout, orch.ModelCatalog()) == model_catalog()
