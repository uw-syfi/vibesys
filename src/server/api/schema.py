"""Export the wire contract as JSON Schema for generated clients."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from pydantic import BaseModel

from server.api.protocol import ProtocolRequest, Response, RunSnapshot, ServerMessage
from server.events import RunEvent

_EXPECTED_ARGUMENT_COUNT = 2


class ProtocolDocument(BaseModel):
    """Root schema document for the public server protocol."""

    request: ProtocolRequest
    response: Response
    event: RunEvent
    snapshot: RunSnapshot
    server_message: ServerMessage


def main() -> None:
    """Write the public protocol JSON schema to the requested path."""
    if len(sys.argv) != _EXPECTED_ARGUMENT_COUNT:
        message = "usage: python -m server.api.schema OUTPUT.json"
        raise SystemExit(message)
    output = Path(sys.argv[1])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(ProtocolDocument.model_json_schema(), indent=2) + "\n")


if __name__ == "__main__":
    main()
