"""Identities the strategy declares to core: its id and the reply schema of each role."""

from vs_core.api import SchemaRef, StrategyId

STRATEGY_ID = StrategyId(root="dynamic")

PLANNER_REPLY = SchemaRef(name="dynamic.planner-reply", version=1)
IMPLEMENTER_REPLY = SchemaRef(name="dynamic.implementer-reply", version=1)
JUDGE_REPLY = SchemaRef(name="dynamic.judge-reply", version=1)
PROFILER_REPLY = SchemaRef(name="dynamic.profiler-reply", version=1)
