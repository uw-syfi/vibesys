"""Identities the strategy declares to core: its id and the reply schema of each role."""

from vs_core.api import SchemaRef, StrategyId

STRATEGY_ID = StrategyId(root="multi")

PRE_ROUND_REPLY = SchemaRef(name="multi.pre-round-reply", version=1)
PROFILER_REPLY = SchemaRef(name="multi.profiler-reply", version=1)
PLANNER_REPLY = SchemaRef(name="multi.planner-reply", version=1)
IMPLEMENTER_REPLY = SchemaRef(name="multi.implementer-reply", version=1)
JUDGE_REPLY = SchemaRef(name="multi.judge-reply", version=1)
