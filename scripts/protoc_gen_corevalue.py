"""Run the repository-local core value protoc plugin."""

from protobuf.plugin import Schema, run

from scripts.corevalue.api import Options, compile_files


def generate(schema: Schema[Options]) -> None:
    """Write the pure compiler's per-file outputs through protoc's plugin transport."""
    for path, content in compile_files(schema.files_to_generate, schema.options):
        out = schema.generate_file(path)
        for line in content.splitlines():
            out.print(line)


if __name__ == "__main__":
    run("protoc-gen-corevalue", "0.1.0", Options, generate)
