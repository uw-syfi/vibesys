"""Public contract of the versioned Slurm connector request and response documents."""

from __future__ import annotations

import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_decode.api import Unreadable, ValidationOptions, decode_json
from vs_slurm.api import (
    MAX_CONNECTOR_REQUEST_BYTES,
    MAX_CONNECTOR_RESPONSE_BYTES,
    SLURM_CONNECTOR_REQUEST,
    SlurmConnectorExecRequest,
    SlurmConnectorResponse,
)


@given(command=st.text(alphabet="abc XYZ012/-_", min_size=1, max_size=80))
def test_an_exec_request_round_trips_through_the_public_wire_contract(command: str) -> None:
    request = SlurmConnectorExecRequest(version=1, operation="exec", command=command)

    decoded = decode_json(
        request.model_dump_json(),
        SLURM_CONNECTOR_REQUEST,
        source="test request",
        max_bytes=MAX_CONNECTOR_REQUEST_BYTES,
        options=ValidationOptions(strict=True),
    )

    assert decoded == request


@pytest.mark.parametrize(
    "document",
    [
        {"operation": "exec", "command": "hostname"},
        {"version": 2, "operation": "exec", "command": "hostname"},
        {"version": 1, "operation": "remove", "remote_path": "/runs/job"},
        {"version": 1, "operation": "exec", "command": "hostname", "secret": "value"},
        {
            "version": 1,
            "operation": "put",
            "local_path": "relative/input",
            "remote_path": "/runs/job/input",
        },
        {
            "version": 1,
            "operation": "get",
            "remote_path": "/runs/job/../secret",
            "local_path": "/var/output",
            "kind": "file",
        },
    ],
)
def test_unknown_versions_operations_fields_and_malformed_paths_are_rejected(
    document: dict[str, object],
) -> None:
    decoded = decode_json(
        json.dumps(document),
        SLURM_CONNECTOR_REQUEST,
        source="test request",
        max_bytes=MAX_CONNECTOR_REQUEST_BYTES,
        options=ValidationOptions(strict=True),
    )

    assert isinstance(decoded, Unreadable)


def test_a_connector_response_rejects_unknown_fields_and_versions() -> None:
    decoded = decode_json(
        '{"version":2,"returncode":0,"stdout":"","stderr":"","extra":true}',
        SlurmConnectorResponse,
        source="test response",
        max_bytes=MAX_CONNECTOR_RESPONSE_BYTES,
        options=ValidationOptions(strict=True),
    )

    assert isinstance(decoded, Unreadable)


def test_connector_response_bound_counts_the_complete_json_envelope() -> None:
    empty = SlurmConnectorResponse(version=1, returncode=0, stdout="", stderr="")
    overhead = len(empty.model_dump_json().encode())
    at_limit = empty.model_copy(update={"stdout": "x" * (MAX_CONNECTOR_RESPONSE_BYTES - overhead)})
    encoded = at_limit.model_dump_json().encode()

    assert len(encoded) == MAX_CONNECTOR_RESPONSE_BYTES
    accepted = decode_json(
        encoded,
        SlurmConnectorResponse,
        source="test response",
        max_bytes=MAX_CONNECTOR_RESPONSE_BYTES,
        options=ValidationOptions(strict=True),
    )
    oversized = at_limit.model_copy(update={"stdout": f"{at_limit.stdout}x"})
    refused = decode_json(
        oversized.model_dump_json().encode(),
        SlurmConnectorResponse,
        source="test response",
        max_bytes=MAX_CONNECTOR_RESPONSE_BYTES,
        options=ValidationOptions(strict=True),
    )

    assert accepted == at_limit
    assert isinstance(refused, Unreadable)
