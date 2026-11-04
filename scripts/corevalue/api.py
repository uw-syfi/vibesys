"""Pure protoc descriptor compilation; unsupported guarantees fail by name."""

from protobuf import DescFile

from scripts.corevalue._compiler import compile_files
from scripts.corevalue._schema import GenerationError, Options

__all__ = ["GenerationError", "Options", "compile_boundary", "compile_files", "compile_values"]


def compile_values(file: DescFile) -> str:
    """Compile immutable values from one descriptor, rejecting unsupported guarantees."""
    return compile_files([file], Options())[0][1]


def compile_boundary(file: DescFile, options: Options | None = None) -> str:
    """Compile the matching protobuf conversions independently of protoc I/O."""
    return compile_files([file], options or Options(boundary=True))[0][1]
