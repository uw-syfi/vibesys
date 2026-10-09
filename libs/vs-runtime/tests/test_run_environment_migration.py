"""Properties of migrating a recorded run environment on resume."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from vs_project.api import RunEnvironmentRecord, RunResourceRequest
from vs_runtime.api.infrastructure import migrate_recorded_run_environment

_TEXT = st.one_of(st.none(), st.text(min_size=1, max_size=12))
_RESOURCES = st.one_of(
    st.none(),
    st.builds(
        RunResourceRequest,
        nodes=st.integers(1, 4),
        accelerators_per_node=st.integers(1, 8),
        accelerator_backend=st.sampled_from(("cuda", "rocm", "trainium")),
    ),
)
_RECORDS = st.builds(
    RunEnvironmentRecord,
    name=st.sampled_from(("local", "docker", "host", "modal", "skypilot", "slurm", "slurm-gpu")),
    image=_TEXT,
    gpu=_TEXT,
    model_volume=_TEXT,
    app=_TEXT,
    resources=_RESOURCES,
)


@given(_RECORDS)
def test_no_migrated_record_names_the_local_environment(record: RunEnvironmentRecord) -> None:
    assert migrate_recorded_run_environment(record).name != "local"


@given(_RECORDS)
def test_migration_is_idempotent(record: RunEnvironmentRecord) -> None:
    once = migrate_recorded_run_environment(record)

    assert migrate_recorded_run_environment(once) == once


@given(_RECORDS)
def test_only_the_environment_name_of_a_local_record_changes(record: RunEnvironmentRecord) -> None:
    migrated = migrate_recorded_run_environment(record)

    if record.name == "local":
        assert migrated == record.model_copy(update={"name": "docker"})
    else:
        assert migrated == record
