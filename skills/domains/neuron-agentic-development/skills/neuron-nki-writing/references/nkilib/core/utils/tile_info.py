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


from dataclasses import dataclass
from typing import Optional

import nki.language as nl
from nki.language import NKIObject

from .kernel_assert import kernel_assert
from .kernel_helpers import get_ceil_quotient


#
# Basic tiled dimension info
#
@dataclass
class TiledDimInfo(NKIObject):
    """
    Private
    """

    @staticmethod
    def build(tiled_dim_size: int, tile_size: int, subtile_info: "TiledDimInfo" = None) -> "TiledDimInfo":
        tile_count = get_ceil_quotient(tiled_dim_size, tile_size)
        return TiledDimInfo(tiled_dim_size, tile_size, tile_count, subtile_info)

    """
    Public
    """
    # Size of the dimension being tiled
    tiled_dim_size: int
    # The size of each tile
    tile_size: int
    # The number of tiles needed to cover the dimension being tiled
    tile_count: int
    # Subtile information (if there is any)
    subtile_dim_info: "Optional[TiledDimInfo]" = None

    # Factory methods
    # ONLY CONSTRUCT THIS USING THE FACTORY METHODS BELOW

    # Build a subtiled version
    @staticmethod
    def build_with_subtiling(tiled_dim_size: int, tile_size: int, subtile_size: int) -> "TiledDimInfo":
        subtiled_dim_info = TiledDimInfo.build(tile_size, subtile_size)
        return TiledDimInfo.build(tiled_dim_size, tile_size, subtiled_dim_info)

    def is_subtiled(self) -> bool:
        return self.subtile_dim_info != None

    # Calculate indices for the tile given a tile number and offset
    # TODO: Now this only works if last item from mgrid.
    # Fix once mgrid is properly supported.
    def get_tile_indices(self, tile_num, tile_offset):
        return nl.ds(tile_num * self.tile_size, tile_offset)

    # Same idea as the above method but also factor in subtiles
    # TODO: Now this only works if last item from mgrid.
    # Fix once mgrid is properly supported.
    def get_subtile_indices(self, tile_num, subtile_num, subtile_offset):
        kernel_assert(self.is_subtiled(), "Error: This tiled dimension has no subtiles")
        return nl.ds(
            tile_num * self.tile_size + subtile_num * self.subtile_dim_info.tile_size,
            subtile_offset,
        )

    # Calculate the start position for a subtile
    def get_subtile_start(self, tile_idx, subtile_idx):
        kernel_assert(self.is_subtiled(), "Error: This tiled dimension has no subtiles")
        return tile_idx * self.tile_size + subtile_idx * self.subtile_dim_info.tile_size

    # Calculate the local start position for a subtile (within a loaded tile)
    def get_local_subtile_start(self, subtile_idx):
        kernel_assert(self.is_subtiled(), "Error: This tiled dimension has no subtiles")
        return subtile_idx * self.subtile_dim_info.tile_size

    # Calculate the valid bound for a subtile given the total dimension size
    def get_subtile_bound(self, tile_idx, subtile_idx):
        kernel_assert(self.is_subtiled(), "Error: This tiled dimension has no subtiles")
        subtile_start = self.get_subtile_start(tile_idx, subtile_idx)
        return min(self.tiled_dim_size - subtile_start, self.subtile_dim_info.tile_size)

    # Calculate the local valid bound for a subtile (within a loaded tile)
    def get_local_subtile_bound(self, tile_idx, subtile_idx):
        kernel_assert(self.is_subtiled(), "Error: This tiled dimension has no subtiles")
        local_start = self.get_local_subtile_start(subtile_idx)
        tile_bound = self.get_tile_bound(tile_idx)
        return min(self.subtile_dim_info.tile_size, tile_bound - local_start)

    # Calculate the valid bound for a tile given the total dimension size
    def get_tile_bound(self, tile_idx):
        tile_start = tile_idx * self.tile_size
        return min(self.tiled_dim_size - tile_start, self.tile_size)

    # Calculate the actual number of subtiles in the tile of tile_idx
    def get_actual_subtile_num(self, tile_idx):
        kernel_assert(self.is_subtiled(), "Error: This tiled dimension has no subtiles")
        return get_ceil_quotient(self.get_tile_bound(tile_idx), self.subtile_dim_info.tile_size)
