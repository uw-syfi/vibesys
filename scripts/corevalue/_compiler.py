"""Pure descriptor compilation, independent of protoc's I/O."""

from __future__ import annotations

from typing import TYPE_CHECKING

from scripts.corevalue._boundary import boundary_file
from scripts.corevalue._emit import value_file
from scripts.corevalue._validate import validate_file

if TYPE_CHECKING:
    from collections.abc import Sequence

    from protobuf import DescFile

    from scripts.corevalue._schema import Options


def compile_files(files: Sequence[DescFile], options: Options) -> tuple[tuple[str, str], ...]:
    """Compile each requested descriptor to its checked-in per-file value or boundary module."""
    outputs = []
    for file in files:
        if file.name.endswith("value_options.proto"):
            continue
        validate_file(file)
        suffix = "_boundary.py" if options.boundary else "_cv.py"
        content = boundary_file(file, options) if options.boundary else value_file(file)
        outputs.append((file.name.removesuffix(".proto") + suffix, content))
    return tuple(outputs)
