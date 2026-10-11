"""Identities the strategy declares to core: its id and the reply schema of each role."""

from vs_core.api import SchemaRef, StrategyId

STRATEGY_ID = StrategyId(root="single")

PLANNER_REPLY = SchemaRef(name="single.planner-reply", version=1)
IMPLEMENTER_REPLY = SchemaRef(name="single.implementer-reply", version=1)
