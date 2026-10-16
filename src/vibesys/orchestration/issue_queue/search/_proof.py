"""The accuracy proof the final candidate needs before core will adopt it.

The root workspace stays at the input until adoption, so the closed issues' work lives in the
retained candidate of the last of them (each issue builds on the one before). Core adopts a
retained candidate only with an accepted accuracy proof when the task measures accuracy, so the
run measures the final candidate's accuracy once, at the end. Whether the proof was accepted is
core's to decide at adoption; a measurement that ended without one simply leaves the adoption to
be refused, which ends the run with core's typed reason.
"""

from vibesys.search_spans.api import GATE, GateAttrs
from vs_core.api.search import Flow, Kept, Label, MeasurementRecipe


async def prove(ctx: Flow, tip: Kept) -> None:
    """Measure the accuracy of ``tip`` when the task's gates make adopting it depend on that."""
    terms = ctx.task.terms
    stage = terms.accuracy_stage
    if not terms.requires_accuracy_proof or stage is None:
        return
    recipe = MeasurementRecipe.of(ctx.facts)
    plan = recipe.plan(tip.revision, purpose="official", now_at=ctx.clock.now(), stages=(stage,))
    label = Label(GATE, title=stage, attrs=GateAttrs(stage=stage))
    await ctx.evaluation.measure(plan, within=recipe.budget((stage,)), label=label)
