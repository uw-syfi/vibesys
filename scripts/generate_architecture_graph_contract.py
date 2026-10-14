"""Generate the language-neutral architecture graph descriptor and TS module."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from vibesys.api.architecture import ArchitectureGraphDocument


def architecture_graph_json_schema() -> dict[str, object]:
    """Return the authoritative client-generation document."""
    return ArchitectureGraphDocument.model_json_schema()


def schema_typescript() -> str:
    """Embed the descriptor for the strict TypeScript boundary parser."""
    document = json.dumps(architecture_graph_json_schema(), indent=2)
    return (
        "/* Generated from vibesys.api.architecture. Do not edit. */\n\n"
        f"const architectureGraphSchema: Record<string, unknown> = {document};\n\n"
        "export default architectureGraphSchema;\n"
    )


def main() -> None:
    """Write generated architecture-graph artifacts to requested paths."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--module", type=Path, required=True)
    arguments = parser.parse_args()
    arguments.schema.parent.mkdir(parents=True, exist_ok=True)
    arguments.module.parent.mkdir(parents=True, exist_ok=True)
    arguments.schema.write_text(
        json.dumps(architecture_graph_json_schema(), indent=2) + "\n", encoding="utf-8"
    )
    arguments.module.write_text(schema_typescript(), encoding="utf-8")


if __name__ == "__main__":
    main()
