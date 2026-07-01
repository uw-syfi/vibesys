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

from .kernel_assert import kernel_assert


def stream_shuffle_broadcast(src, dst):
    """
    Broadcasts the first partition of src onto the partition dim of dst.

    All inputs and outputs to this function are assumed to be in sbuf.
    This requires 2D src and dst, and the final dim of src matching the final dim of dst.

    :param src: 2D input tensor
    :param dst: 2D output tensor
    """
    dst_npar = dst.shape[0]
    kernel_assert(len(src.shape) == 2 and len(dst.shape) == 2, "src and dst must be 2D tensors")
    kernel_assert(src.shape[1] == dst.shape[1], "src and dst must have matching final dimension")

    shuffle_mask = [0] * 32
    for i in range((dst_npar + 31) // 32):
        cur_npar = min(32, dst_npar - i * 32)
        nisa.nc_stream_shuffle(
            src=src[0:1, :],
            dst=dst[i * 32 : i * 32 + cur_npar, 0 : dst.shape[1]],
            shuffle_mask=shuffle_mask,
        )
