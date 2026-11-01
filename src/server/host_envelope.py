"""What every host helper request and reply carries: the fields the channel itself reads.

A request names itself (``request_id``) and how long the client waits (``deadline_ms``); a reply
names the request it answers. Each message of the helper, in ``server.host_api`` and in the
contract modules it imports (``server.host_run_summary``), derives from these two bases, so a
feature's messages can live in a module of their own without reaching into ``host_api``.

The optional capabilities ``hello`` negotiates (``HelperFeature``) live here too: each gates a request
or an optional field of one, and ``server.host_api`` says which.

Light: Pydantic and the standard library only.

Public surface: ``HelperErrorCode``, ``HelperFeature``, ``RequestId``, ``RunId``, ``INSTANCE_ID_PATTERN``, ``InstanceId``,
``SpanId``, ``MAX_DEADLINE_MS``, ``HelperRequestBase`` and ``HelperReplyBase``.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

RequestId = Annotated[str, StringConstraints(min_length=1, max_length=128)]
"""Chosen by the client, unique while the channel owns its request or watch worker."""

RunId = Annotated[str, StringConstraints(min_length=1)]
"""The durable run a server drives; ``vs_project`` owns its full syntax."""

INSTANCE_ID_PATTERN = r"^[0-9a-f]{12}$"
InstanceId = Annotated[str, StringConstraints(pattern=INSTANCE_ID_PATTERN)]
"""A registry key: 12 lowercase hex digits, so it is never a path fragment."""

SpanId = Annotated[str, StringConstraints(pattern=r"^(flow|op):\S*$")]
"""A run-trace span's identity, as the run protocol's ``SpanId``."""

MAX_DEADLINE_MS = 3_600_000


class HelperFeature(StrEnum):
    """Optional capabilities negotiated by ``hello``; a closed set per protocol version.

    A client calls a gated method only after ``hello`` returned its feature, so
    a strict peer never sees a method it does not know.
    """

    INSTANCES_WATCH = "instances_watch"
    """``instances.watch`` and ``instances.unwatch``: registry changes pushed as they happen."""
    PATHS_COMPLETE = "paths_complete"
    """``paths.complete``: directory completion for a typed path on the host."""
    INSTANCES_INSPECT = "instances_inspect"
    """``instances.inspect``: retained metadata and an optional bounded log tail."""
    LOGS = "logs"
    """``logs.list`` and ``logs.read``: a run's recorded log streams, read in bounded chunks."""
    LOGS_CANCEL = "logs_cancel"
    """``logs.cancel``: withdraw a waiting ``logs.read`` so its worker returns at once."""
    HARNESSES = "harnesses"
    """``harnesses.list`` and ``run.start``'s ``harness``: start a run on a vetted provider and model."""
    LOGS_BY_SPAN = "logs_by_span"
    """``logs.list``'s ``span_id``: only the streams whose owner names that run-trace span."""
    RUN_HISTORY = "run_history"
    """``runs.read_events``: a run's recorded events, read from disk in bounded pages."""
    RUN_SUMMARY = "run_summary"
    """``runs.summary``'s pages (``offset``): everything the run level shows, from a read model kept
    beside the journal."""
    RUN_HISTORY_PAGES = "run_history_pages"
    """``runs.read_events``'s pages of up to ``RUN_EVENTS_READ_MAX`` events, and its ``with_totals``."""
    RUN_SPAN_EVENTS = "run_span_events"
    """``runs.events_for_span``: the recorded events of one span and the spans beneath it."""
    RUN_TAIL = "run_tail"
    """``runs.read_tail``: the newest recorded events before a sequence, in bounded pages."""
    RUN_TRACE_EVENTS = "run_trace_events"
    """``runs.read_events``'s ``kinds``: only the events of one kind, such as the trace's, read from
    the journal's span index so the rest of a long journal is neither read nor sent."""
    MACHINE_DESCRIBE = "machine_describe"
    """``machine.describe``: the host's checkout, hardware, Docker, harness CLIs and run counts."""
    CHECKOUT_INSPECT = "checkout_inspect"
    """``checkout.inspect``: whether a path holds a VibeSys checkout this app can use."""
    PREFLIGHT = "preflight"
    """``run.preflight``: the closed checks that say whether a run can start, each with its fix."""
    RUN_OPTIONS = "run_options"
    """``run.start``'s and ``run.preflight``'s ``options``: rounds, time limit, results, roles."""
    EXAMPLES = "examples"
    """``examples.list``, ``project.create_from_example`` and ``project.copy_out``."""
    RUN_ROLES = "run_roles"
    """``run.preflight``'s ``with_roles``: the role ids the run's orchestration declares."""
    PROJECT_CLEAN = "project_clean"
    """``run.preflight``'s ``with_project_clean``: the ``project_clean`` check, which fails with a
    typed ``project_dirty`` cause naming the changed paths."""
    RUN_BACKEND = "run_backend"
    """``run.preflight``'s ``with_backend``: the compute backend the run resolved to (the task's),
    and each failing check's typed ``cause``."""
    RUN_CAUSES = "run_causes"
    """``run.preflight``'s ``with_causes``: the typed ``cause`` of a failed ``settings_valid``
    check beyond an unrunnable backend (the task, configuration, agent and argument failures)."""
    EXAMPLE_CATALOGUE = "example_catalogue"
    """``examples.list``'s ``with_catalogue``: each task's domain, served model, hardware,
    workload, objectives and goal text, in ``task_details``."""
    RUN_DIGEST = "run_digest"
    """``runs.list``'s ``digest_runs``: each named run's ``digest``, read from its journal."""
    RUN_DASHBOARD = "run_dashboard"
    """``runs.list``'s ``with_dashboard``: each digested run's per-idea ``dashboard`` points and its
    ``attention`` reason, for the runs dashboard's tiles."""
    HARNESS_GENERATIONS = "harness_generations"
    """``harnesses.list``'s ``with_generations``: each pair's maker and the generation it belongs to."""
    RUN_ROLE_AGENTS = "run_role_agents"
    """``run.start``'s and ``run.preflight``'s ``agents``: a vetted provider and model per role."""
    MEASUREMENT_TARGET = "measurement_target"
    """``run.start``'s and ``run.preflight``'s ``measure_on``: where results are measured, and
    the ``measurement_target`` check."""
    MACHINE_COMPUTE = "machine_compute"
    """``compute.list``, ``compute.test_cluster``, ``compute.add_cluster`` and
    ``compute.remove_cluster``: the machine's Slurm clusters and Modal token."""
    AGENT_LOGINS = "agent_logins"
    """``agent_logins.list`` and ``agent_logins.start``: each harness's login on the machine."""
    PROJECT_GOAL = "project_goal"
    """``project.task_goal``: a project's task goal text (``OBJECTIVE.md``) and its objectives."""
    EXAMPLE_DESTINATION = "example_destination"
    """``examples.destination``: what is at a place an example would be copied to."""
    RUN_TRACE = "run_trace"
    """``runs.summary``: its reply names ``trace`` and ``rounds_finished``, so a client that reads
    them (a journal that predates the run trace has no ideas, not zero) needs this to send it."""
    RUN_ENDING = "run_ending"
    """``runs.summary``'s ``with_ending``: the reply names ``input_gates`` and ``terminal``, the gates
    that finished before the first idea and the run's whole failure message, which a run opened
    from its summary has no events to read them from."""
    CHAT_ENDED_RUNS = "chat_ended_runs"
    """``run.reader``: the server Chat reads a run through, which for a run with no live server is
    one started read-only on demand and ended when no client holds it."""
    CHAT_ENDED_RUN_QUESTIONS = "chat_ended_run_questions"
    """The read-only server of a finished run answers chat questions and opens chat threads over the
    saved run (``query.chat``, ``query.chat_thread_create``); every command stays refused."""


class HelperRequestBase(BaseModel):
    """The fields every helper request has, strict: an unknown field is rejected."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: RequestId
    deadline_ms: Annotated[int, Field(gt=0, le=MAX_DEADLINE_MS)]
    """How long the client waits for the reply; the helper answers ``deadline_exceeded`` after it."""


class HelperReplyBase(BaseModel):
    """The field every helper reply has, strict: an unknown field is rejected."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: RequestId


class HelperErrorCode(StrEnum):
    """Why a request failed; a closed set."""

    HANDSHAKE_REQUIRED = "handshake_required"
    """A request other than ``hello`` came before a successful ``hello``."""
    INCOMPATIBLE = "incompatible"
    """No protocol version both sides speak, or ``hello`` was sent twice."""
    INVALID_REQUEST = "invalid_request"
    """The line is not a request of the negotiated version."""
    DUPLICATE_REQUEST_ID = "duplicate_request_id"
    """Another request or watch with this id is open on the channel."""
    FEATURE_NOT_NEGOTIATED = "feature_not_negotiated"
    """The method needs a feature ``hello`` did not return."""
    DEADLINE_EXCEEDED = "deadline_exceeded"
    """The reply was not ready within the request's ``deadline_ms``."""
    CHANNEL_BUSY = "channel_busy"
    """The channel's worker limit is full, or ``run.connect`` needs an idle channel."""
    UNKNOWN_WATCH = "unknown_watch"
    """``instances.unwatch`` named no open watch."""
    PROJECT_MISSING = "project_missing"
    """The project directory does not exist or is not a directory."""
    PROJECT_UNINITIALIZED = "project_uninitialized"
    """The directory has no ``.vibesys/tasks``."""
    TASK_INVALID = "task_invalid"
    """The project's tasks or runs could not be read."""
    PATH_MISSING = "path_missing"
    """The directory to complete in does not exist."""
    PATH_NOT_DIRECTORY = "path_not_directory"
    """The path to complete in is not a directory."""
    PERMISSION_DENIED = "permission_denied"
    """This user may not list the directory."""
    RUN_GONE = "run_gone"
    """No live server has the id, or nothing listens at its socket."""
    CONNECT_DENIED = "connect_denied"
    """The run's socket exists but this user may not connect to it."""
    CONNECT_FAILED = "connect_failed"
    """Connecting to the run's socket failed another way."""
    UNKNOWN_INSTANCE = "unknown_instance"
    """``logs.list`` or ``logs.read`` named an instance this host recorded no logs for."""
    UNKNOWN_LOG_STREAM = "unknown_log_stream"
    """``logs.read`` named a stream the instance does not have."""
    LOG_STORE_CORRUPT = "log_store_corrupt"
    """``logs.list`` or ``logs.read`` found the run's log index corrupt; asking again does not help."""
    UNKNOWN_EXAMPLE = "unknown_example"
    """``project.create_from_example`` named no example, or one ambiguously; the message lists."""
    EXAMPLES_UNAVAILABLE = "examples_unavailable"
    """The helper runs from no checkout with an example catalog, or the catalog is invalid."""
    DESTINATION_EXISTS = "destination_exists"
    """A new project's destination exists (and, for an example, is not an empty directory)."""
    DESTINATION_NESTED = "destination_nested"
    """A new project's destination lies inside a Git repository or the VibeSys checkout."""
    NOT_NESTED = "not_nested"
    """``project.copy_out``'s source is in no other Git repository: it can run where it is."""
    PROJECT_CREATE_FAILED = "project_create_failed"
    """Copying or committing failed; nothing is left at the destination, and the message says why."""
    UNKNOWN_RUN = "unknown_run"
    """``runs.read_events`` or ``runs.summary`` named a run the project does not have; the message names it."""
    UNVETTED_HARNESS = "unvetted_harness"
    """``run.start``'s ``harness`` is not one ``harnesses.list`` offers; only sent with ``harnesses``."""
    UNKNOWN_CLUSTER = "unknown_cluster"
    """``compute.remove_cluster`` or ``measure_on`` named a cluster not set up; the message names it."""
    CLUSTER_EXISTS = "cluster_exists"
    """``compute.add_cluster`` named a cluster already set up on this machine."""
    CLUSTER_READ_ONLY = "cluster_read_only"
    """``compute.remove_cluster`` named a cluster of a file this helper only reads."""
    CLUSTER_TEST_FAILED = "cluster_test_failed"
    """``compute.add_cluster``'s cluster did not pass its connection test; nothing was saved."""
    CLUSTER_INVALID = "cluster_invalid"
    """A cluster's settings are rejected by the Slurm configuration; the message names the keys."""
    COMPUTE_CONFIG_UNREADABLE = "compute_config_unreadable"
    """The compute settings file cannot be read or parsed; ``path`` names it."""
    MODAL_NOT_CONFIGURED = "modal_not_configured"
    """``run.start``'s ``measure_on`` is Modal and the machine holds no token."""
    UNKNOWN_PROVIDER = "unknown_provider"
    """``agent_logins.start`` named a provider with no harness here; the message names it."""
    READER_FAILED = "reader_failed"
    """``run.reader`` could not start the read-only server of a run with no live one; the message says why."""
    INTERNAL = "internal"
    """The helper failed; ``message`` says how."""
