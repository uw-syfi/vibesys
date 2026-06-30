# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import ast
import inspect
import textwrap
from typing import Callable, Generic, TypeAlias, TypeVar

from .kernel_assert import kernel_assert

TorchRefProtocolT = TypeVar("TorchRefProtocolT")
TorchRefProtocolType: TypeAlias = type[TorchRefProtocolT]


class LncSubscriptable(Generic[TorchRefProtocolT]):
    def __init__(self, func: Callable, protocol: TorchRefProtocolType):
        self._func = func
        self._lnc: int = 0
        self._shard_id: int = 0
        _verify_protocol_sync(func, protocol)

    def __getitem__(self, lnc: int) -> TorchRefProtocolT:
        def wrapper(*args, **kwargs):
            self._lnc = lnc
            return self._func(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    def __call__(self, *args, **kwargs):
        raise TypeError(
            f"{self._func.__name__} must be subscripted with LNC value. "
            f"Usage: {self._func.__name__}[lnc](q=..., k_active=..., ...)"
        )

    @property
    def lnc(self) -> int:
        """Get the current LNC value."""
        return self._lnc

    @property
    def shard_id(self) -> int:
        """Get the current shard ID (0 to lnc-1)."""
        return self._shard_id

    @shard_id.setter
    def shard_id(self, value: int) -> None:
        """Set the shard ID to simulate."""
        self._shard_id = value


def _verify_protocol_sync(func: Callable, protocol: TorchRefProtocolType):
    func_sig = inspect.signature(func)
    protocol_sig = inspect.signature(protocol.__call__)

    func_params = list(func_sig.parameters.items())
    protocol_params = [(k, v) for k, v in protocol_sig.parameters.items() if k != "self"]

    kernel_assert(
        len(func_params) == len(protocol_params),
        f"Parameter count mismatch: function has {len(func_params)}, Protocol has {len(protocol_params)}",
    )

    for (fn, fp), (pn, pp) in zip(func_params, protocol_params):
        kernel_assert(fn == pn, f"Parameter name mismatch: function has '{fn}', Protocol has '{pn}'")
        kernel_assert(
            fp.annotation == pp.annotation,
            f"Parameter '{fn}' annotation mismatch: function has {fp.annotation}, Protocol has {pp.annotation}",
        )

    kernel_assert(
        func_sig.return_annotation == protocol_sig.return_annotation,
        f"Return annotation mismatch: function has {func_sig.return_annotation}, "
        f"Protocol has {protocol_sig.return_annotation}",
    )

    # Get variable name from assignment target in caller's frame
    frame = inspect.currentframe()
    try:
        caller_frame = frame.f_back.f_back
        module = inspect.getmodule(caller_frame)
        source = inspect.getsource(module)
        lineno = caller_frame.f_lineno
        var_name = _find_assignment_target(source, lineno)
        if var_name is None:
            raise RuntimeError(f"Could not find assignment target at line {lineno}")
        var_doc = _extract_var_docstring(source, var_name)
        if var_doc is None:
            raise RuntimeError(f"Could not find docstring for variable '{var_name}'")
    finally:
        del frame

    protocol_doc = protocol.__call__.__doc__
    diff_str = _compare_docstrings(var_doc, protocol_doc)
    if diff_str:
        kernel_assert(False, f"Docstring mismatch between variable and Protocol:\n{diff_str}")


def _find_assignment_target(source: str, lineno: int) -> str | None:
    """Find the variable name being assigned at or containing the given line."""
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if node.lineno <= lineno <= node.end_lineno:
                if isinstance(node.targets[0], ast.Name):
                    return node.targets[0].id
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            if node.lineno <= lineno <= node.end_lineno:
                if isinstance(node.target, ast.Name):
                    return node.target.id
    return None


def _extract_var_docstring(source: str, var_name: str) -> str | None:
    """Extract the docstring following a variable declaration."""
    tree = ast.parse(source)
    for i, node in enumerate(tree.body):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == var_name:
                if i + 1 < len(tree.body):
                    next_node = tree.body[i + 1]
                    if isinstance(next_node, ast.Expr) and isinstance(next_node.value, ast.Constant):
                        if isinstance(next_node.value.value, str):
                            return next_node.value.value
    return None


def _compare_docstrings(var_doc: str | None, protocol_doc: str | None) -> str | None:
    """Compare docstrings after normalizing indentation. Returns diff string if mismatch, None if match."""
    import difflib

    var_doc_normalized = textwrap.dedent(var_doc or "").strip()
    protocol_doc_normalized = textwrap.dedent(protocol_doc or "").strip()
    if var_doc_normalized != protocol_doc_normalized:
        diff = difflib.unified_diff(
            var_doc_normalized.splitlines(keepends=True),
            protocol_doc_normalized.splitlines(keepends=True),
            fromfile="variable",
            tofile="protocol",
        )
        return "".join(diff)
    return None
