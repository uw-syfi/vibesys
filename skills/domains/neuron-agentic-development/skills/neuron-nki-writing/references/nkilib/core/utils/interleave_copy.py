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


import nki.isa as nisa
import nki.language as nl

from .kernel_assert import kernel_assert
from .tensor_view import TensorView


def _require_broadcast(tensor):
    """Check if tensor requires broadcasting for vector engine compatibility."""
    return len(tensor.shape) == 2 and tensor.shape[1] == 1


def _is_vector_shaped(tensor):
    """Check if tensor is vector-shaped for activation engine compatibility."""
    return len(tensor.shape) == 1 or (len(tensor.shape) == 2 and tensor.shape[1] == 1)


def interleave_copy(
    dst: nl.ndarray,
    src: nl.ndarray,
    scale: TensorView = None,
    bias: TensorView = None,
    index: int = 0,
):
    """
    Copy data from `src` to `dst` using interleaved execution between scalar and vector engines.
    Optionally applies scale and bias transformations during the copy operation.

    Args:
        dst: Destination tensor
        src: Source tensor
        scale: Optional scaling factor (supports vector format for activation engine)
        bias: Optional bias term (supports vector format for activation engine)
        index: Engine selection index (even=activation engine, odd=vector engine)

    Note: Activation engine requires vector-shaped tensors (1D or 2D with shape[1]==1).
    """

    # Determine tensor properties
    use_activation_scale = scale is not None and _is_vector_shaped(scale)
    use_activation_bias = bias is not None and _is_vector_shaped(bias)
    use_even_engine = index % 2 == 0

    # Validate dimensions
    if scale is not None:
        kernel_assert(
            dst.shape[0] == src.shape[0] == scale.shape[0],
            f"Partition dimension must match across dst, src, and scale.",
        )
    if bias is not None:
        kernel_assert(
            dst.shape[0] == src.shape[0] == bias.shape[0], f"Partition dimension must match across dst, src, and bias."
        )

    # Pure copy operation
    if scale is None and bias is None:
        if use_even_engine:
            nisa.activation(dst=dst, data=src, op=nl.copy)
        else:
            nisa.tensor_copy(dst=dst, src=src, engine=nisa.vector_engine)
        return

    # Both scale and bias present with vector shapes - use activation engine when possible
    if use_activation_scale and use_activation_bias:
        if use_even_engine:
            nisa.activation(dst=dst, data=src, scale=scale.get_view(), bias=bias.get_view(), op=nl.copy)
        else:
            # Vector engine: broadcast and apply operations sequentially
            scale = scale.broadcast(dim=1, size=src.shape[-1])
            bias = bias.broadcast(dim=1, size=src.shape[-1])
            nisa.tensor_tensor(dst=dst, data1=src, data2=scale.get_view(), op=nl.multiply)
            nisa.tensor_tensor(dst=dst, data1=dst, data2=bias.get_view(), op=nl.add)
        return

    # Scale only
    if scale is not None and bias is None:
        if use_activation_scale and use_even_engine:
            nisa.activation(dst=dst, data=src, scale=scale.get_view(), op=nl.copy)
        else:
            # Use vector engine for non-vector scale or odd index
            if _require_broadcast(scale):
                scale = scale.broadcast(dim=1, size=src.shape[-1])
            nisa.tensor_tensor(dst=dst, data1=src, data2=scale.get_view(), op=nl.multiply)
        return

    # Bias only
    if bias is not None and scale is None:
        if use_activation_bias and use_even_engine:
            nisa.activation(dst=dst, data=src, bias=bias.get_view(), op=nl.copy)
        else:
            # Use vector engine for non-vector bias or odd index
            if _require_broadcast(bias):
                bias = bias.broadcast(dim=1, size=src.shape[-1])
            nisa.tensor_tensor(dst=dst, data1=src, data2=bias.get_view(), op=nl.add)
        return

    # Mixed scale and bias (at least one non-vector shaped)
    if bias is not None and scale is not None:
        # First apply scale
        if use_activation_scale:
            nisa.activation(dst=dst, data=src, scale=scale.get_view(), op=nl.copy)
        else:
            nisa.tensor_tensor(dst=dst, data1=src, data2=scale.get_view(), op=nl.multiply)

        # Then apply bias
        if use_activation_bias:
            nisa.activation(dst=dst, data=dst, bias=bias.get_view(), op=nl.copy)
        else:
            nisa.tensor_tensor(dst=dst, data1=dst, data2=bias.get_view(), op=nl.add)
        return
