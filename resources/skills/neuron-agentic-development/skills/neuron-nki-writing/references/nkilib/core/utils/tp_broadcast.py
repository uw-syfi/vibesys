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

"""
This file contains an implementation of transpose broadcast, moving a column tensor into every parition of a destination
tensor.

"""

import nki.isa as nisa
import nki.language as nl

from .kernel_assert import kernel_assert


def tp_broadcast(src, dst, src_offset, psum_address=None):
    """
    Transposes then broadcasts src[0:1, :] onto all partitions of dst
    Using a single transpose instruction (on PE) and repeated input access to broadcast src to dst
    Each partition of dst will become a transposed version of src.

    WARNING: This function will always broadcast src[0:1], it will throw error if
    a slice is passed in.

    All inputs and outputs to this function are assumed to be in sbuf.
    This requires 2D src [P, 1] and dst [broad_cast_dim, P]
    where the first dim of src must match the second dim of dst.
    Uses a psum bank for the transpose

    Args:
        src: 2D input tensor. Shape: [P, F]
        dst: 2D output tensor. Shape: [B, P]
        src_offset: Specify the offset in F to take the column from
    """
    p_dim, f_dim = src.shape
    broadcast_dim, tp_dim = dst.shape

    kernel_assert(tp_dim == p_dim, "Transposed dim didn't match")

    # Transpose and broadcast into intermediate psum buffer
    tp_psum = nl.ndarray((broadcast_dim, tp_dim), nl.float32, buffer=nl.psum, address=psum_address)

    # FIXME: This always broadcast src[0:1, :] due to limitation of nested indexing
    nisa.nc_transpose(
        tp_psum[...], src.ap([[f_dim, p_dim], [0, broadcast_dim]], offset=src_offset)
    )  # Use repeated access to broadcast

    # Copy back to sbuf
    nisa.tensor_copy(dst[0:broadcast_dim, 0:tp_dim], src=tp_psum)
