"""Identities the strategy declares to core: its id and the reply schema of each role."""

from vs_core.api import SchemaRef, StrategyId

STRATEGY_ID = StrategyId(root="plain")

IMPLEMENTER_REPLY = SchemaRef(name="plain.implementer-reply", version=1)
JUDGE_REPLY = SchemaRef(name="plain.judge-reply", version=1)
PERF_REPLY = SchemaRef(name="plain.perf-evaluator-reply", version=1)
