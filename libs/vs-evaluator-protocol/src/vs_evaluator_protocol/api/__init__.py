"""Public evaluator result protocol.

``Hello``, ``MetricSpec``, ``Result``, and ``ErrorRecord`` define the versioned
record stream. ``parse_records`` validates its lines; ``read_measurement``
reduces the stream to a ``Measurement``. ``check_objectives`` validates metric
names required by a caller. Invalid streams raise ``ProtocolError`` with a
``ReasonCode``. Evaluator execution and scoring belong to the caller.
"""

from vs_evaluator_protocol.errors import ProtocolError, ReasonCode
from vs_evaluator_protocol.measurement import (
    Measurement,
    check_objectives,
    read_measurement,
)
from vs_evaluator_protocol.records import (
    PROTOCOL_VERSION,
    ErrorRecord,
    Hello,
    MetricSpec,
    Record,
    Result,
    parse_records,
)

__all__ = [
    "PROTOCOL_VERSION",
    "ErrorRecord",
    "Hello",
    "Measurement",
    "MetricSpec",
    "ProtocolError",
    "ReasonCode",
    "Record",
    "Result",
    "check_objectives",
    "parse_records",
    "read_measurement",
]
