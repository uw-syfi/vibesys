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

"""PyTorch reference implementation for indexed_flatten kernel."""

import torch


def indexed_flatten_torch_ref(
    input_tensor: torch.Tensor,
    f_len: int,
    output_len: int,
    row_offsets: torch.Tensor,
    padding_val: int = -1,
) -> torch.Tensor:
    """
    PyTorch reference implementation of indexed_flatten.

    This is a reference implementation for testing the NKI indexed_flatten kernel.
    For input_tensor of shape [E, T], reshapes to [E, T//f_len, f_len] and writes
    each row's data into output at the specified row_offsets (block offsets).

    Args:
        input_tensor (torch.Tensor): [E, T], Input tensor
        f_len (int): Block size in free dimension
        output_len (int): Length of output array
        row_offsets (torch.Tensor): [E,], Block offsets for each row
        padding_val (int): Value for unwritten positions (default: -1)

    Returns:
        torch.Tensor: [output_len,], Flattened output tensor

    Note:
        Hardware-specific parameters (LNC sharding) are not included as they
        don't affect the mathematical result.
    """
    E, T = input_tensor.shape
    partitions_per_row = T // f_len
    num_output_blocks = output_len // f_len

    output = torch.full((output_len,), padding_val, dtype=input_tensor.dtype, device=input_tensor.device)
    output_blocks = output.reshape(num_output_blocks, f_len)
    input_reshaped = input_tensor.reshape(E, partitions_per_row, f_len)

    for row_idx in range(E):
        block_offset = row_offsets[row_idx].item()
        for partition_idx in range(partitions_per_row):
            out_block_idx = block_offset + partition_idx
            if 0 <= out_block_idx < num_output_blocks:
                output_blocks[out_block_idx, :] = input_reshaped[row_idx, partition_idx, :]

    return output
