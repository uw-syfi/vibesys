"""Canonical schema identity derived from embedded source descriptors."""

import hashlib
from importlib import import_module
from importlib.resources import files

from protobuf.wkt.google.protobuf.descriptor_pb import FileDescriptorSet


def schema_hash(image: bytes, package: str) -> str:
    """Hash a package's sorted descriptors, excluding source locations/comments.

    Imported packages are versioned separately. All options, including validation
    constraints, are retained. Missing packages raise ValueError.
    """
    selected = [
        file for file in FileDescriptorSet.from_binary(image).file if file.package == package
    ]
    if not selected:
        message = f"Descriptor set has no files in package {package}"
        raise ValueError(message)
    for file in selected:
        file.clear_field("source_code_info")
    canonical = FileDescriptorSet(file=sorted(selected, key=lambda file: file.name))
    return hashlib.sha256(canonical.to_binary()).hexdigest()


def embedded_hash(package: str) -> str:
    namespace = f"vs_proto._generated.{package}"
    descriptors = [
        import_module(f"{namespace}.{path.name.removesuffix('.py')}").desc().proto
        for path in files(namespace).iterdir()
        if path.name.endswith("_pb.py")
    ]
    return schema_hash(FileDescriptorSet(file=descriptors).to_binary(), package)


API_SCHEMA_HASH = embedded_hash("vibesys.api.v1")
STORE_SCHEMA_HASH = embedded_hash("vibesys.store.v1")

HOST_SCHEMA_HASH = embedded_hash("vibesys.host.v1")
