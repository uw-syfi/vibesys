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
TiledRange - Helper function/class for tiling dimensions (remainder logic)

TiledRange divides a dimension into tiles and provides a tuple of iterators.
TiledRangeIterator represents a single tile with size, index, and start_offset properties.

"""

import math
from typing import Tuple, Union

from nki.language import NKIObject


class TiledRangeIterator(NKIObject):
    """
    Represents a single tile in a tiled range.

    - size: The size of this tile
    - index: The index of this tile in the range
    - start_offset: Absolute starting offset (i.e., subtile offset is calculated properly)
    - end_offset: Absolute ending offset (i.e., subtile offset is calculated properly)
    """

    def __init__(self, tile_size: int, tile_index: int, start_offset: int, end_offset: int):
        """
        Initialize a TiledRangeIterator.

        Args:
            tile_size: The size of this tile
            tile_index: The index of this tile (0-based)
            start_offset: The starting offset in the original dimension
            end_offset: The ending offset in the original dimension
        """
        self.size = tile_size
        self.index = tile_index
        self.start_offset = start_offset
        self.end_offset = end_offset

    def __repr__(self) -> str:
        return f"TiledRangeIterator(size={self.size}, index={self.index}, start_offset={self.start_offset}, end_offset={self.end_offset})"


def TiledRange(size: Union[int, TiledRangeIterator], tile_size: int) -> Tuple[TiledRangeIterator, ...]:
    """
    Divides a dimension into tiles and returns a tuple of TiledRangeIterators.

    Args:
        size: Either an integer representing the total size to tile,
              or a TiledRangeIterator for nested tiling
        tile_size: The size of each tile

    Returns:
        A tuple of TiledRangeIterator objects

    Example:
        >>> tiles = TiledRange(300, 128)
        >>> for tile in tiles:
        ...     print(f"size={tile.size}, index={tile.index}, start_offset={tile.start_offset}")
        size=128, index=0, start_offset=0, end_offset=128
        size=128, index=1, start_offset=128, end_offset=256
        size=44, index=2, start_offset=256, end_offset=300

    Supports nested tiling:
        >>> outer_tiles = TiledRange(300, 128)
        >>> for outer_tile in outer_tiles:
        ...     inner_tiles = TiledRange(outer_tile, 64)
        ...     for inner_tile in inner_tiles:
        ...         print(f"outer_idx={outer_tile.index}, inner_idx={inner_tile.index}, size={inner_tile.size}")
    """
    if isinstance(size, TiledRangeIterator):
        # Subtiled case: use the size of the TiledRangeIterator
        total_size = size.size
        base_offset = size.start_offset
    else:
        total_size = size
        base_offset = 0

    # Calculate the number of tiles needed (ceiling division)
    num_tiles = math.ceil(total_size / tile_size)

    # Create the tuple of TiledRangeIterators
    iterators = []
    for i in range(num_tiles):
        relative_offset = i * tile_size
        # Last tile may be smaller
        current_tile_size = min(tile_size, total_size - relative_offset)
        # Add base_offset to make the start_offset absolute
        start_offset = base_offset + relative_offset
        end_offset = base_offset + relative_offset + current_tile_size
        iterators.append(TiledRangeIterator(current_tile_size, i, start_offset, end_offset))

    return tuple(iterators)
