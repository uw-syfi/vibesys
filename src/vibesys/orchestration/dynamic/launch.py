"""The options a dynamic run is launched with: the search's config and the task's inputs.

One flat set of keys is what the operator writes and what a run records, so a resume restores
them by name. The search reads only `config`; the host reads the rest (`TaskInputs`). Unknown
keys are rejected.
"""

from __future__ import annotations

from pydantic import model_validator

from vibesys.orchestration.agent_options import TaskInputs
from vibesys.orchestration.dynamic.search.api import MAX_WIDTH, DynamicConfig


class DynamicLaunch(TaskInputs, DynamicConfig):
    """Search config and task inputs of one dynamic run.

    ``profiling`` is not the operator's to set: it follows the run's evaluation plan (a profile
    command and a chosen profiler), which only the host knows (see `dynamic_core`). ``plan`` is
    the task's ``[plan]`` table, which the launcher copies from the input bundle; ``mode`` is the
    operator's choice to follow it, refused at once when the task declares none.
    """

    @model_validator(mode="after")
    def _profiling_follows_the_plan(self) -> DynamicLaunch:
        if "profiling" in self.model_fields_set:
            msg = "'profiling' is not a launch option: it follows the run's evaluation plan"
            raise ValueError(msg)
        return self

    def search_config(self, *, profiling: bool, evaluation_capacity: int) -> DynamicConfig:
        """The part the search reads, with what the run environment allows.

        ``profiling`` follows the evaluation plan. ``evaluation_capacity`` is how many evaluation
        jobs the run environment's executor admits at once: the run holds that many trusted
        evaluations unless the operator chose ``evaluation_slots``. An exploring run is not given
        the task's plan, so it reads exactly what it did before plans existed.
        """
        fields = {name: getattr(self, name) for name in DynamicConfig.model_fields}
        plan = self.plan if self.follows_plan else None
        slots = self.evaluation_slots or min(max(evaluation_capacity, 1), MAX_WIDTH)
        resolved = {"profiling": profiling, "plan": plan, "evaluation_slots": slots}
        return DynamicConfig.model_validate({**fields, **resolved})
