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
Modular tensor allocator for multi-buffering patterns in SBUF.

This utility simplifies the allocation of SBUF tensors in loops where addresses follow
a modular pattern for circular buffering. Common in attention kernels where tiles
are reused across loop iterations.

See `ModularAllocator.alloc_sbuf_tensor` for examples.

"""

from typing import List, Tuple, Union

import nki.language as nl

from .allocator import align_to as align_to_fn
from .allocator import sizeinbytes
from .kernel_assert import kernel_assert


class ModularAllocator(nl.NKIObject):
    """
    A class-based modular allocator that manages SBUF memory allocation with modular
    addressing patterns for circular buffering.

    This allocator helps manage memory allocation in SBUF, tracking the current address
    and providing utilities for allocation with modular patterns.

    Example usage:
      allocator = ModularAllocator()

      # Allocate tensors without passing sca around
      k_loaded = allocator.alloc_sbuf_tensor(
        block_dim=[16],
        shape=(128, 512),
        dtype=nl.bfloat16,
        num_free_tiles=[4]
      )

      # Get current address if needed
      current_addr = allocator.get_current_address()

      # Set address if needed
      allocator.set_current_address(new_addr)
    """

    def __init__(self, initial_address: int = 0):
        """
        Initialize the ModularAllocator.

        Args:
          initial_address: Starting address for SBUF allocations (default: 0)
        """
        self._current_address = initial_address

    def get_current_address(self) -> int:
        """
        Get the current SBUF address.

        Returns:
          The current address pointer
        """
        return self._current_address

    def set_current_address(self, address: int):
        """
        Set the current SBUF address.

        Args:
          address: The address to set as the current allocation pointer
        """
        self._current_address = address

    def alloc_sbuf_tensor(
        self,
        shape: Tuple[int, ...],
        dtype,
        block_dim: List[int] = None,
        num_free_tiles: List[int] = None,
        base_partition: int = 0,
        align_to: int = None,
    ) -> Union[List, object]:
        """
        Allocate SBUF tensors with modular address pattern for circular buffering.

        This function creates nested lists of tensors where each dimension can use modular
        addressing for circular buffering. The address pattern follows:
          address[i][j] = sca + ((i % num_free_tiles[0]) * stride0 +
                                 (j % num_free_tiles[1]) * stride1) * tile_size_bytes

        Note: The first element of 'shape' is the partition dimension and is NOT included
        in the address calculation. Only the free dimensions (shape[1:]) contribute to
        the tile size.

        Args:
          shape: Shape of each individual tensor (e.g., (128, 512))
                 First element is the partition dimension, remaining are free dimensions
          dtype: Data type of tensors (e.g., nl.bfloat16, nl.float32)
          block_dim: List defining the nested list structure (e.g., [num_grps, num_tiles])
                     This is the logical size of each dimension.
                     If None (default), allocates a single tensor.
          num_free_tiles: List of modulo factors for each dimension in block_dim
                          e.g., [2, 4] means dim 0 uses mod 2, dim 1 uses mod 4
                          If a dimension should not use modular allocation, set it equal
                          to the corresponding block_dim value.
                          If None, defaults to block_dim (no modular allocation).
          base_partition: Base partition index (default: 0)
          align_to: Optional alignment value. If provided, aligns the current address
                    to this boundary before allocation (default: None, no alignment)

        Returns:
          - If block_dim is None or empty: returns a single tensor
          - If block_dim has 1 element: returns a flat list
          - If block_dim has N elements: returns N-dimensional nested list

          The current address is automatically updated after allocation.

        Example 1: Simple single tensor allocation
          # Allocate a single tensor (block_dim defaults to None)
          tensor = allocator.alloc_sbuf_tensor(
            shape=(128, 512),
            dtype=nl.bfloat16
          )

        Example 2: Simple 1D allocation with modular pattern
          # Allocate 16 K tiles but only use 4 physical buffers
          k_loaded = allocator.alloc_sbuf_tensor(
            shape=(128, 512),
            dtype=nl.bfloat16,
            block_dim=[16],
            num_free_tiles=[4]
          )
          # k_loaded[0], k_loaded[4], k_loaded[8], k_loaded[12] share the same address

        Example 3: 2D allocation with alignment
          # Allocate with 32-byte alignment
          k_loaded = allocator.alloc_sbuf_tensor(
            shape=(128, 2048),
            dtype=nl.float32,
            block_dim=[num_grps, num_2048_tiles],
            num_free_tiles=[2, num_2048_tiles],  # Only grp_i uses modular (mod 2)
            align_to=32
          )
          # k_loaded[0][j] and k_loaded[2][j] share the same address
        """
        # Apply alignment if requested
        if align_to != None:
            self._current_address = align_to_fn(self._current_address, align_to)

        # Handle default block_dim
        if block_dim == None:
            block_dim = []

        # Handle default num_free_tiles
        if num_free_tiles == None:
            num_free_tiles = block_dim.copy() if block_dim else []

        kernel_assert(
            len(block_dim) == len(num_free_tiles),
            f"block_dim length ({len(block_dim)}) must match num_free_tiles length ({len(num_free_tiles)})",
        )

        # Calculate tile size in bytes (only free dimensions, not partition dimension)
        tile_elements = 1
        for dim in shape[1:]:  # Skip first element (partition dimension)
            tile_elements *= dim
        itemsize = sizeinbytes(dtype)
        tile_size_bytes = tile_elements * itemsize

        # Handle empty block_dim case - return a single tensor
        if len(block_dim) == 0:
            tensor = nl.ndarray(
                shape=shape,
                dtype=dtype,
                buffer=nl.sbuf,
                address=(base_partition, self._current_address),
            )
            self._current_address += tile_size_bytes
            return tensor

        # Calculate total physical tiles needed
        total_physical_tiles = 1
        for i in range(len(num_free_tiles)):
            total_physical_tiles *= num_free_tiles[i]

        # Allocate using current address
        nested_list = _allocate_recursive(
            [],
            0,
            block_dim,
            num_free_tiles,
            shape,
            dtype,
            base_partition,
            self._current_address,
            tile_size_bytes,
        )

        # Update current address after allocation
        self._current_address += total_physical_tiles * tile_size_bytes

        return nested_list


def _allocate_recursive(
    indices: List[int],
    depth: int,
    block_dim: List[int],
    num_free_tiles: List[int],
    shape: Tuple[int, ...],
    dtype,
    base_partition: int,
    sca: int,
    tile_size_bytes: int,
):
    """Recursively build nested list structure for modular allocation."""
    if depth == len(block_dim):
        # Base case: allocate actual tensor
        # Calculate address based on modular pattern
        addr_offset = 0
        stride = 1
        for dim_idx in range(len(block_dim) - 1, -1, -1):
            idx = indices[dim_idx] % num_free_tiles[dim_idx]
            addr_offset += idx * stride
            stride *= num_free_tiles[dim_idx]

        tensor = nl.ndarray(
            shape=shape,
            dtype=dtype,
            buffer=nl.sbuf,
            address=(base_partition, sca + addr_offset * tile_size_bytes),
        )
        return tensor
    else:
        # Recursive case: build list at current depth
        result = []
        for i in range(block_dim[depth]):
            result.append(
                _allocate_recursive(
                    indices + [i],
                    depth + 1,
                    block_dim,
                    num_free_tiles,
                    shape,
                    dtype,
                    base_partition,
                    sca,
                    tile_size_bytes,
                )
            )
        return result
