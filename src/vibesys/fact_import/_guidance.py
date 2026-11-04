"""Fold shared guidance facts by occurrence while preserving all source proof."""

from vs_proto.api import Oneof
from vs_proto.api import imported_facts as pb

from ._source import FactImportError


def consolidate_guidance(facts: list[pb.ImportedFact]) -> list[pb.ImportedFact]:
    retained: list[pb.ImportedFact] = []
    guidance: dict[str, pb.ImportedFact] = {}
    for fact in facts:
        if fact.value is None or fact.value.field != "guidance":
            retained.append(fact)
            continue
        identity = fact.value.value.identity
        previous = guidance.get(identity)
        if previous is None:
            guidance[identity] = fact
        else:
            if previous.value is None or previous.value.field != "guidance":
                raise FactImportError(identity, "invalid guidance accumulator")
            combined = _combine(previous.value.value, fact.value.value, fact.imported_from[0].path)
            guidance[identity] = pb.ImportedFact(
                value=Oneof("guidance", combined),
                imported_from=[*previous.imported_from, *fact.imported_from],
            )
    return [*retained, *(guidance[key] for key in sorted(guidance))]


def _combine(
    previous: pb.ImportedGuidance, current: pb.ImportedGuidance, path: str
) -> pb.ImportedGuidance:
    for name in (
        "text",
        "artifact",
        "target",
        "mode",
        "historical_target_kind",
        "historical_target_id",
    ):
        left, right = getattr(previous, name), getattr(current, name)
        if left and right and left != right:
            raise FactImportError(path, f"guidance {current.identity}: conflicting {name}")
    return pb.ImportedGuidance(
        identity=current.identity,
        text=previous.text or current.text,
        artifact=previous.artifact or current.artifact,
        target=previous.target or current.target,
        mode=previous.mode or current.mode,
        historical_target_kind=(previous.historical_target_kind or current.historical_target_kind)
        or None,
        historical_target_id=(previous.historical_target_id or current.historical_target_id)
        or None,
        delivered=previous.delivered or current.delivered,
        disposed=previous.disposed or current.disposed,
        withdrawn=previous.withdrawn or current.withdrawn,
        possibly_read=previous.possibly_read or current.possibly_read,
    )
