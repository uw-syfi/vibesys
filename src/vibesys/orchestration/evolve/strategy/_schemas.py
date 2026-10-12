"""Identities the strategy declares to core: its id and the reply schema of each role."""

from vs_core.api import SchemaRef, StrategyId

STRATEGY_ID = StrategyId(root="evolve")

MUTATOR_REPLY = SchemaRef(name="evolve.mutator-reply", version=1)
JUDGE_REPLY = SchemaRef(name="evolve.judge-reply", version=1)
PROFILER_REPLY = SchemaRef(name="evolve.profiler-reply", version=1)
