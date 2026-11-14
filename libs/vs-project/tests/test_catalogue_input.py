"""The ``[catalogue]`` table of ``vibesys.input.toml`` is held to the facts its domain requires."""

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from vs_project.api import (
    AcceleratorVendor,
    CatalogueInput,
    ExampleDomain,
    HardwareInput,
    ModelMaker,
    ServedModelInput,
    WorkloadInput,
)

_HARDWARE = HardwareInput(accelerator="CPU", count=1, vendor=AcceleratorVendor.CPU)
_MODEL = ServedModelInput(name="Llama 3.3 70B", maker=ModelMaker.META)
_WORKLOAD = WorkloadInput(name="mpmc", description="Many producers and many consumers.")


_FACTS: dict[str, object] = {"model": _MODEL, "workload": _WORKLOAD}


def _required(domain: ExampleDomain) -> dict[str, object]:
    """The facts a domain requires: a model for serving, a workload for data structures."""
    return {
        "model": _MODEL if domain is ExampleDomain.LLM_SERVING else None,
        "workload": _WORKLOAD if domain is ExampleDomain.DATA_STRUCTURES else None,
    }


@pytest.mark.regression
@pytest.mark.parametrize("spelling", ["cli-tools", "kernel-writing"])
def test_a_cli_tools_or_kernel_writing_task_can_be_catalogued(spelling: str) -> None:
    catalogue = CatalogueInput.model_validate({"domain": spelling, "hardware": _HARDWARE})

    assert catalogue.domain.value == spelling


@given(st.sampled_from(ExampleDomain))
def test_every_domain_is_accepted_with_exactly_the_facts_it_requires(domain: ExampleDomain) -> None:
    required = _required(domain)
    table = {"domain": domain.value, "hardware": _HARDWARE}

    assert CatalogueInput.model_validate({**table, **required}).domain is domain
    for name, fact in required.items():
        wrong = {**required, name: None if fact is not None else _FACTS[name]}
        with pytest.raises(ValidationError, match=f"{name} is required"):
            CatalogueInput.model_validate({**table, **wrong})
