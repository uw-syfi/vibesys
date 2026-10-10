"""The AST identity recorded in baselines reads the same under every supported Python."""

import ast

from hypothesis import given
from hypothesis import strategies as st
from scripts.ast_identity import node_identity

NAMES = st.sampled_from(["f", "self.run", "client.close", "load"])
ARGUMENTS = st.lists(st.sampled_from(["x", "1", "'s'", "y.z"]), max_size=2)
KEYWORDS = st.lists(st.sampled_from(["a=1", "b=x", "c=None"]), max_size=2, unique=True)


def test_a_call_without_arguments_keeps_the_python_312_spelling() -> None:
    call = ast.parse("f()", mode="eval").body

    assert node_identity(call) == "Call(func=Name(id='f', ctx=Load()), args=[], keywords=[])"


@given(NAMES, ARGUMENTS, KEYWORDS)
def test_every_list_field_is_spelled_out_even_when_empty(
    name: str, arguments: list[str], keywords: list[str]
) -> None:
    call = ast.parse(f"{name}({', '.join([*arguments, *keywords])})", mode="eval").body
    identity = node_identity(call)

    for node in ast.walk(call):
        for field, value in ast.iter_fields(node):
            if isinstance(value, list):
                assert f"{field}=" in identity, (type(node).__name__, field)
