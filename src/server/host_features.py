"""The optional capabilities a helper channel negotiates with ``hello``.

Each feature gates a method or an optional field (``server.host_api``'s
``HELPER_REQUEST_FEATURES`` and ``HELPER_FIELD_FEATURES`` say which), so a client calls a
gated message only after ``hello`` returned its feature and a strict peer never sees one it
does not know. Light: the standard library only.

Public surface: ``HelperFeature``.
"""

from __future__ import annotations

from enum import StrEnum


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
    """``runs.summary``: everything the run level shows, from a read model kept beside the journal."""
    RUN_HISTORY_PAGES = "run_history_pages"
    """``runs.read_events``'s pages of up to ``RUN_EVENTS_READ_MAX`` events, and its ``with_totals``."""
    RUN_SPAN_EVENTS = "run_span_events"
    """``runs.events_for_span``: the recorded events of one span and the spans beneath it."""
    RUN_TAIL = "run_tail"
    """``runs.read_tail``: the newest recorded events before a sequence, in bounded pages."""
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


__all__ = ["HelperFeature"]
