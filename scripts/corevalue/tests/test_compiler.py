"""Compile the real descriptors in process and reject unsupported shapes before emission."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st

from scripts.corevalue.api import (
    GenerationError,
    Options,
    compile_boundary,
    compile_files,
    compile_values,
)
from scripts.corevalue.tests.api import MapValue, encode
from vs_proto.api import core_state, core_value_options

if TYPE_CHECKING:
    from collections.abc import Callable

    from protobuf import DescFile


@pytest.mark.parametrize("compiler", [compile_values, compile_boundary])
@pytest.mark.parametrize(
    "file", [core_state.ControlInput.desc().file, encode(MapValue()).desc().file]
)
def test_the_real_schema_compiles_to_complete_python(
    compiler: Callable[[DescFile], str], file: DescFile
) -> None:
    source = compiler(file)
    compile(source, "<core-values>", "exec")
    assert source == compiler(file)


@given(reserved=st.sampled_from(["model_fields", "model_copy", "model_dump_json", "_hash"]))
def test_constructor_method_collisions_are_rejected_by_field_name(reserved: str) -> None:
    original = core_state.ControlInput.desc()
    bad_field = replace(original.fields[0], name=reserved, local_name=reserved)
    message = replace(original, fields=[bad_field])
    file = replace(original.file, messages=[message])
    with pytest.raises(GenerationError, match=reserved):
        compile_values(file)


def test_colliding_generated_names_fail_before_a_file_is_emitted() -> None:
    original = core_state.ControlInput.desc()
    file = replace(original.file, messages=[original, original])
    with pytest.raises(GenerationError, match="collision: ControlInput"):
        compile_values(file)


def test_representation_metadata_is_not_emitted_as_core_state() -> None:
    file = core_value_options.ValueMessageOptions.desc().file
    assert compile_files([file], Options()) == ()
