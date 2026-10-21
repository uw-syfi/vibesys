/** Typed span facts join the existing comparison and timing projections by declared links. */
import {fromBinary} from '@bufbuild/protobuf';
import {
  BaselineAttrsSchema,
  BaselineStatus,
  CapacityCondition,
  CapacityWaitAttrsSchema,
  ComparisonAttrsSchema,
  ComparisonVerdict,
  Direction,
  GateAttrsSchema,
  GateStage,
  GateSubject,
  SpanStatus,
  WorkstreamAttrsSchema,
} from '@vibesys/proto';
import type {ComparisonBaseline, CoreState, MeasurementComparison} from '../state.js';
import type {StoreHistory, StoreView} from './types.js';

export interface CapacityInterval {
  readonly from: number;
  readonly to: number;
  readonly provider: string;
  readonly condition: 'quota_exhausted' | 'rate_limited' | 'unavailable' | null;
}
const VERDICTS: Partial<Record<ComparisonVerdict, MeasurementComparison['verdict']>> = {
  [ComparisonVerdict.BETTER]: 'better',
  [ComparisonVerdict.WITHIN_NOISE]: 'within_noise',
  [ComparisonVerdict.WORSE]: 'worse',
  [ComparisonVerdict.INCONCLUSIVE]: 'inconclusive',
};
const BASELINES: Partial<Record<BaselineStatus, ComparisonBaseline['status']>> = {
  [BaselineStatus.MEASURED]: 'measured',
  [BaselineStatus.FAILED]: 'failed',
  [BaselineStatus.NOT_CONFIGURED]: 'not_configured',
  [BaselineStatus.NOT_MEASURED]: 'not_measured',
};
const CONDITIONS: Partial<Record<CapacityCondition, CapacityInterval['condition']>> = {
  [CapacityCondition.QUOTA_EXHAUSTED]: 'quota_exhausted',
  [CapacityCondition.RATE_LIMITED]: 'rate_limited',
  [CapacityCondition.UNAVAILABLE]: 'unavailable',
};

function revision(view: StoreView, id: string | undefined): string | undefined {
  const attrs = id === undefined ? undefined : view.spans[id]?.label?.attrs;
  if (!attrs) return undefined;
  const type = attrs.typeUrl.split('/').at(-1);
  if (type === WorkstreamAttrsSchema.typeName)
    return fromBinary(WorkstreamAttrsSchema, attrs.value).revision;
  if (
    type === GateAttrsSchema.typeName &&
    fromBinary(GateAttrsSchema, attrs.value).subject === GateSubject.INPUT
  )
    return view.epochs.at(-1)?.epoch?.run?.input;
  return undefined;
}

export function comparisonFacts(view: StoreView): CoreState['comparisons'] {
  const comparisons = Object.create(null) as Record<string, MeasurementComparison>;
  for (const span of Object.values(view.spans)) {
    const attrs = span.label?.attrs;
    if (attrs?.typeUrl.split('/').at(-1) !== ComparisonAttrsSchema.typeName) continue;
    const candidate = revision(view, span.label?.links.find(link => link.rel === 'candidate')?.to);
    const base = revision(view, span.label?.links.find(link => link.rel === 'base')?.to);
    const reading = fromBinary(ComparisonAttrsSchema, attrs.value);
    const verdict = VERDICTS[reading.verdict];
    if (!candidate || !base || !verdict) continue;
    comparisons[candidate] = {
      verdict,
      ratio: reading.ratio,
      metric: reading.metric,
      candidateValue: reading.candidateValue,
      baseValue: reading.baseValue,
      baseCommit: base,
      candidateSamples: reading.candidateSamples,
      baseSamples: reading.baseSamples,
    };
  }
  return comparisons;
}

export function baselineFacts(view: StoreView): readonly ComparisonBaseline[] {
  return Object.values(view.spans).flatMap(span => {
    const attrs = span.label?.attrs;
    if (attrs?.typeUrl.split('/').at(-1) !== BaselineAttrsSchema.typeName) return [];
    const reading = fromBinary(BaselineAttrsSchema, attrs.value);
    const status = BASELINES[reading.status];
    if (!status) return [];
    return [
      {
        name: span.id,
        label: reading.label,
        status,
        metric: reading.metric ?? null,
        unit: reading.unit ?? null,
        direction:
          reading.direction === Direction.MAX
            ? ('max' as const)
            : reading.direction === Direction.MIN
              ? ('min' as const)
              : null,
        value: reading.value ?? null,
        spread: reading.spread ?? null,
        samples: reading.samples ?? null,
        hosts: reading.hosts,
        reason: reading.reason ?? null,
      },
    ];
  });
}

export function capacityIntervals(view: StoreView): readonly CapacityInterval[] {
  return Object.values(view.spans).flatMap(span => {
    const attrs = span.label?.attrs;
    if (
      attrs?.typeUrl.split('/').at(-1) !== CapacityWaitAttrsSchema.typeName ||
      span.endedAt === undefined
    )
      return [];
    const wait = fromBinary(CapacityWaitAttrsSchema, attrs.value);
    return [
      {
        from: span.startedAt,
        to: span.endedAt,
        provider: wait.provider,
        condition: CONDITIONS[wait.condition] ?? null,
      },
    ];
  });
}

const GATES: Partial<Record<GateStage, string>> = {
  [GateStage.BUILD]: 'build',
  [GateStage.ACCURACY]: 'accuracy',
  [GateStage.BENCHMARK]: 'benchmark',
  [GateStage.VALIDATION]: 'validation',
};
export function inputFailures(history: StoreHistory, view: StoreView) {
  const gates = Object.values(view.spans).flatMap(span => {
    const attrs = span.label?.attrs;
    if (attrs?.typeUrl.split('/').at(-1) !== GateAttrsSchema.typeName) return [];
    const gate = fromBinary(GateAttrsSchema, attrs.value);
    const stage = GATES[gate.stage];
    if (gate.subject !== GateSubject.INPUT || !stage) return [];
    const sequence = Number(
      history.spans[span.id]?.versions.filter(item => item.seq <= view.seq).at(-1)?.seq ?? 0n,
    );
    return [{gate: stage, recipe: gate.recipe ?? null, sequence, status: span.status}];
  });
  return gates
    .filter(gate => gate.status === SpanStatus.FAILED)
    .map(gate => ({
      gate: gate.gate,
      recipe: gate.recipe,
      sequence: gate.sequence,
      supersededBy:
        gates
          .filter(
            later =>
              later.gate === gate.gate &&
              later.recipe === gate.recipe &&
              later.status === SpanStatus.DONE &&
              later.sequence > gate.sequence,
          )
          .map(later => later.sequence)
          .sort((a, b) => a - b)[0] ?? null,
    }));
}
