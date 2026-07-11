"""Export the wire contract as JSON Schema for generated clients."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from pydantic import BaseModel

from vibe_serve.server.events import RunEvent
from vibe_serve.server.protocol import ProtocolRequest, Response, RunSnapshot, ServerMessage


class ProtocolDocument(BaseModel):
    request: ProtocolRequest
    response: Response
    event: RunEvent
    snapshot: RunSnapshot
    server_message: ServerMessage


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m vibe_serve.server.schema OUTPUT.json")
    output = Path(sys.argv[1])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(ProtocolDocument.model_json_schema(), indent=2) + "\n")


if __name__ == "__main__":
    main()
