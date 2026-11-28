"""FP32 oracle that removes BF16 activation rounding from a task reference.

The graded reference fixes one BF16 rounding schedule. Correctness bands are
calibrated by measuring that reference's own error against this oracle, which
runs the same operator graph with BF16 compute widened to FP32. Quantized
payloads, scales and integer tensors keep their native representation.
"""

from __future__ import annotations

import torch
from torch.utils._python_dispatch import TorchDispatchMode
from torch.utils._pytree import tree_map

aten = torch.ops.aten

# Data movement stays in the input dtype, so slicing or gathering a large BF16
# weight stack does not first widen the whole stack.
_NATIVE = {aten.index.Tensor, aten.index_select.default, aten.gather.default,
           aten.clone.default, aten.contiguous.default, aten.lift_fresh.default,
           aten.lift_fresh_copy.default, aten._local_scalar_dense.default,
           aten.equal.default}


def _widen(value):
    if value is torch.bfloat16:
        return torch.float32
    return value.float() if isinstance(value, torch.Tensor) and value.dtype == torch.bfloat16 else value


def _widen_dtype(value):
    return torch.float32 if value is torch.bfloat16 else value


class WidenBf16(TorchDispatchMode):
    """Execute BF16 compute in FP32; redirect BF16 casts and factories."""

    def __torch_dispatch__(self, func, types, args=(), kwargs=None):
        kwargs = kwargs or {}
        if func.is_view and func.overloadpacket is not aten.to:
            # A dtype argument to a view reinterprets bits; leave it intact.
            # ``aten.to`` appears here only under inference mode, where it
            # is not decomposed into ``_to_copy``.
            return func(*args, **kwargs)
        if (func in _NATIVE or func._schema.is_mutable
                or func.namespace not in ("aten", "prims")):
            return func(*tree_map(_widen_dtype, args), **tree_map(_widen_dtype, kwargs))
        return func(*tree_map(_widen, args), **tree_map(_widen, kwargs))


def fp32_reference(module, case, values: dict[str, torch.Tensor], **options) -> dict[str, torch.Tensor]:
    with torch.no_grad(), WidenBf16():
        return module.reference(case, values, **options)
