"""Compile the real descriptors in process and reject unsupported shapes before emission."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st
from protobuf import Oneof, ScalarType

from scripts.corevalue.api import (
    GenerationError,
    Options,
    compile_boundary,
    compile_files,
    compile_values,
)
from scripts.corevalue.tests.api import MapValue, encode
from vs_proto.api import (
    compiler_values,
    core_contracts,
    core_leaves,
    core_value_options,
    validation_rules,
)
from vs_proto.api import core_control as core_state

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


@pytest.mark.parametrize("compiler", [compile_values, compile_boundary])
@pytest.mark.parametrize(
    "expression",
    [
        "this.records.reduce(x, y, x)",
        "this.records.all(x, x.root.matches(regex))",
        "this.records.all(x, this.records.filter(y, y.root == x.root).unknown())",
        "this.records.all(x, x.root == 'unbound')",
    ],
)
def test_unsupported_list_rules_fail_both_compilers_by_message_name(
    compiler: Callable[[DescFile], str], expression: str
) -> None:
    original = compiler_values.OrderedValues.desc()
    proto = type(original.proto).from_binary(original.proto.to_binary())
    proto.options[validation_rules.ext_message] = validation_rules.MessageRules(
        cel=[validation_rules.Rule(id="unsupported", message="unsupported", expression=expression)]
    )
    message = replace(original, proto=proto)
    with pytest.raises(GenerationError, match="OrderedValues"):
        compiler(replace(original.file, messages=[message]))


@pytest.mark.parametrize("compiler", [compile_values, compile_boundary])
def test_the_shared_leaves_compile_with_all_constructor_rules(
    compiler: Callable[[DescFile], str],
) -> None:
    source = compiler(compiler_values.OrderedValues.desc().file)
    compile(source, "<shared-leaves>", "exec")
    assert source == compiler(compiler_values.OrderedValues.desc().file)


@pytest.mark.parametrize("compiler", [compile_values, compile_boundary])
@pytest.mark.parametrize("default", ["{}", "[]", '"1"', "true", "not-json"])
def test_unrepresentable_defaults_fail_before_either_output_is_emitted(
    compiler: Callable[[DescFile], str], default: str
) -> None:
    original = compiler_values.Ordinal.desc()
    field = original.fields[0]
    proto = type(field.proto).from_binary(field.proto.to_binary())
    proto.options[core_value_options.ext_core_field] = core_value_options.ValueFieldOptions(
        integer=True, default_json=default
    )
    message = replace(original, fields=[replace(field, proto=proto), *original.fields[1:]])
    with pytest.raises(GenerationError, match="Ordinal"):
        compiler(replace(original.file, messages=[message]))


@pytest.mark.parametrize("compiler", [compile_values, compile_boundary])
@given(alias=st.sampled_from(["Count", "OrderedValues", "bad-name", "lowercase"]))
def test_scalar_aliases_reject_collisions_and_invalid_names(
    compiler: Callable[[DescFile], str], alias: str
) -> None:
    original = compiler_values.OrderedValues.desc()
    field = original.fields[-1]
    proto = type(field.proto).from_binary(field.proto.to_binary())
    proto.options[core_value_options.ext_core_field] = core_value_options.ValueFieldOptions(
        aliases=[alias]
    )
    message = replace(original, fields=[*original.fields[:-1], replace(field, proto=proto)])
    with pytest.raises(GenerationError, match="OrderedValues"):
        compiler(replace(original.file, messages=[message]))


@pytest.mark.parametrize("compiler", [compile_values, compile_boundary])
@pytest.mark.parametrize(
    "expression",
    [
        "this.records.all(r, missing.ordinal > 0)",
        "this.records.all(r, this.records.exists(p, p.ordinal == r.ordinal)) && r.ordinal > 0",
        "this.records.all(r, r.misspelled > 0)",
        "this.misspelled.unique()",
        "len(this.records) > 0",
    ],
)
def test_unknown_names_and_python_helpers_fail_closed(
    compiler: Callable[[DescFile], str],
    expression: str,
) -> None:
    original = compiler_values.OrderedValues.desc()
    proto = type(original.proto).from_binary(original.proto.to_binary())
    proto.options[validation_rules.ext_message] = validation_rules.MessageRules(
        cel=[validation_rules.Rule(id="unsupported", expression=expression, message="bad history")]
    )
    with pytest.raises(GenerationError, match="OrderedValues"):
        compiler(replace(original.file, messages=[replace(original, proto=proto)]))


@pytest.mark.parametrize("compiler", [compile_values, compile_boundary])
def test_scalar_union_arms_cannot_overlap_runtime_domains(
    compiler: Callable[[DescFile], str],
) -> None:
    original = core_contracts.PureOptionValue.desc()
    integer = original.fields[1]
    proto = type(integer.proto).from_binary(integer.proto.to_binary())
    proto.options[core_value_options.ext_core_field] = core_value_options.ValueFieldOptions()
    members = [original.fields[0], replace(integer, proto=proto), *original.fields[2:]]
    message = replace(
        original, fields=members, oneofs=[replace(original.oneofs[0], fields=members)]
    )
    with pytest.raises(GenerationError, match="overlapping"):
        compiler(replace(original.file, messages=[message]))


@pytest.mark.parametrize("compiler", [compile_values, compile_boundary])
def test_union_wrapper_rules_cannot_become_decode_only(compiler: Callable[[DescFile], str]) -> None:
    original = core_contracts.PureOptionValue.desc()
    proto = type(original.proto).from_binary(original.proto.to_binary())
    proto.options[validation_rules.ext_message] = validation_rules.MessageRules(
        cel=[
            validation_rules.Rule(
                id="union.rule", expression="size(this.text) > 0", message="nonempty"
            )
        ]
    )
    with pytest.raises(GenerationError, match="union wrapper"):
        compiler(replace(original.file, messages=[replace(original, proto=proto)]))


@pytest.mark.parametrize("compiler", [compile_values, compile_boundary])
@pytest.mark.parametrize("base", ["MissingValue", "PureOptionValue"])
def test_inheritance_requires_a_declared_record_base(
    compiler: Callable[[DescFile], str],
    base: str,
) -> None:
    original = compiler_values.ExtendedQuantity.desc()
    proto = type(original.proto).from_binary(original.proto.to_binary())
    proto.options[core_value_options.ext_core_value] = core_value_options.ValueMessageOptions(
        base=base
    )
    with pytest.raises(GenerationError, match="base value"):
        compiler(replace(original.file, messages=[replace(original, proto=proto)]))


@pytest.mark.parametrize("compiler", [compile_values, compile_boundary])
def test_inherited_fields_cannot_silently_disappear(compiler: Callable[[DescFile], str]) -> None:
    original = compiler_values.ExtendedQuantity.desc()
    message = replace(original, fields=list(original.fields[1:]))
    # Keep the base in the file so this fails on the inherited contract itself.
    messages = [message if m.name == original.name else m for m in original.file.messages]
    with pytest.raises(GenerationError, match="value: inherited contract"):
        compiler(replace(original.file, messages=messages))


@pytest.mark.parametrize("compiler", [compile_values, compile_boundary])
def test_enum_subsets_cannot_include_unspecified_or_unknown_values(
    compiler: Callable[[DescFile], str],
) -> None:
    original = core_leaves.Comparison.desc()
    reason = next(f for f in original.fields if f.name == "verdict")
    proto = type(reason.proto).from_binary(reason.proto.to_binary())
    proto.options[validation_rules.ext_field] = validation_rules.FieldRules(
        type=Oneof("enum", validation_rules.EnumRules(defined_only=True, in_=[0, 999]))
    )
    message = replace(
        original,
        fields=[
            replace(reason, proto=proto) if f.name == reason.name else f for f in original.fields
        ],
    )
    with pytest.raises(GenerationError, match="unrepresented"):
        compiler(replace(original.file, messages=[message]))


@pytest.mark.parametrize("compiler", [compile_values, compile_boundary])
def test_inherited_cel_cannot_silently_disappear(compiler: Callable[[DescFile], str]) -> None:
    original = compiler_values.ExtendedQuantity.desc()
    proto = type(original.proto).from_binary(original.proto.to_binary())
    proto.options[validation_rules.ext_message] = validation_rules.MessageRules()
    messages = [
        replace(m, proto=proto) if m.name == original.name else m for m in original.file.messages
    ]
    with pytest.raises(GenerationError, match="inherited CEL"):
        compiler(replace(original.file, messages=messages))


@pytest.mark.parametrize("compiler", [compile_values, compile_boundary])
def test_cyclic_or_forward_bases_fail_before_emission(compiler: Callable[[DescFile], str]) -> None:
    original = compiler_values.ExtendedQuantity.desc()
    proto = type(original.proto).from_binary(original.proto.to_binary())
    proto.options[core_value_options.ext_core_value] = core_value_options.ValueMessageOptions(
        base=original.name
    )
    messages = [
        replace(m, proto=proto) if m.name == original.name else m for m in original.file.messages
    ]
    with pytest.raises(GenerationError, match="base value must precede"):
        compiler(replace(original.file, messages=messages))


@pytest.mark.parametrize("compiler", [compile_values, compile_boundary])
@pytest.mark.parametrize("shape", ["optional_union", "direct_null"])
def test_null_representations_cannot_lose_wire_presence(
    compiler: Callable[[DescFile], str], shape: str
) -> None:
    original = core_contracts.PureOption.desc()
    field = original.fields[1]
    proto = type(field.proto).from_binary(field.proto.to_binary())
    if shape == "optional_union":
        proto.options[validation_rules.ext_field] = validation_rules.FieldRules()
        field = replace(field, proto=proto)
    else:
        field = replace(field, value=replace(field.value, message=core_contracts.NullValue.desc()))
    message = replace(original, fields=[original.fields[0], field])
    with pytest.raises(GenerationError, match="null"):
        compiler(replace(original.file, messages=[message]))


@pytest.mark.parametrize("compiler", [compile_values, compile_boundary])
@pytest.mark.parametrize(
    ("element", "diagnostic"),
    [
        (ScalarType.FLOAT, "labels: float32 cannot preserve"),
        (ScalarType.DOUBLE, "labels: immutable set requires canonically sortable"),
        (compiler_values.Ordinal.desc(), "labels: immutable set requires canonically sortable"),
    ],
)
def test_immutable_sets_reject_elements_without_a_canonical_sort_order(
    compiler: Callable[[DescFile], str],
    element: object,
    diagnostic: str,
) -> None:
    original = compiler_values.OrderedValues.desc()
    field = next(f for f in original.fields if f.name == "labels")
    modified = replace(field, value=replace(field.value, element=element))
    message = replace(
        original, fields=[modified if f.name == field.name else f for f in original.fields]
    )
    with pytest.raises(GenerationError, match=diagnostic):
        compiler(replace(original.file, messages=[message]))
