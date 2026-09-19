"""Hand-written support for the generated control-protocol messages.

The message classes live in :mod:`server.wire.v2` and are generated from
``proto/server/wire/v2``. This package holds what protobuf cannot express:
strict JSON parsing, semantic validation, legacy journal upgrade, and helpers
for enums and the event payload oneof.
"""

PROTOCOL_VERSION = 2
"""Wire version of every message this server emits and accepts.

Version 1 was the Pydantic/JSON-Schema contract (string discriminators). The
move to protobuf changed the JSON shape, so it is a deliberate break: clients
that send version 1 are rejected with ``protocol_version_unsupported``, and
journals recorded as version 1 are upgraded line by line on load.
"""
