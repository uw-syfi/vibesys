import {create, type MessageInitShape} from '@bufbuild/protobuf';
import {anyPack} from '@bufbuild/protobuf/wkt';
import {
  AdoptionAttrsSchema,
  BaselineAttrsSchema,
  BaselineStatus,
  CapacityCondition,
  CapacityPolicy,
  CapacityWaitAttrsSchema,
  Change,
  ComparisonAttrsSchema,
  ComparisonVerdict,
  Direction,
  GateAttrsSchema,
  GateStage,
  GateSubject,
  type RecordSchema,
  RuntimeType,
  Selection,
  SpanStatus,
} from '@vibesys/proto';

type Body = NonNullable<MessageInitShape<typeof RecordSchema>['body']>;
function inputBodies(): Body[] {
  return [
    {
      case: 'span',
      value: {
        change: Change.ENDED,
        span: {
          id: 'input-gate',
          runtimeType: RuntimeType.FLOW,
          name: 'Input',
          status: SpanStatus.DONE,
          startedAt: 0,
          endedAt: 2,
          label: {
            kind: 'gate',
            title: 'Input',
            attrs: anyPack(
              GateAttrsSchema,
              create(GateAttrsSchema, {stage: GateStage.BENCHMARK, subject: GateSubject.INPUT}),
            ),
          },
        },
      },
    },
    {case: 'metric', value: {series: 'throughput', span: 'input-gate', value: 100, clock: 2}},
  ];
}
function comparisonBody(): Body {
  return {
    case: 'span',
    value: {
      change: Change.ENDED,
      span: {
        id: 'comparison-a',
        runtimeType: RuntimeType.OPERATION,
        name: 'Compare',
        status: SpanStatus.DONE,
        startedAt: 3,
        endedAt: 4,
        label: {
          kind: 'comparison',
          title: 'Compare',
          attrs: anyPack(
            ComparisonAttrsSchema,
            create(ComparisonAttrsSchema, {
              metric: 'throughput',
              unit: 'ops/s',
              direction: Direction.MAX,
              ratio: 1.25,
              candidateValue: 125,
              baseValue: 100,
              candidateSamples: 4,
              baseSamples: 4,
              verdict: ComparisonVerdict.BETTER,
            }),
          ),
          links: [
            {rel: 'candidate', to: 'idea-0'},
            {rel: 'base', to: 'input-gate'},
          ],
        },
      },
    },
  };
}
function baselineBody(): Body {
  return {
    case: 'span',
    value: {
      change: Change.ENDED,
      span: {
        id: 'reference',
        runtimeType: RuntimeType.FLOW,
        name: 'Reference',
        status: SpanStatus.DONE,
        startedAt: 0,
        endedAt: 2,
        label: {
          kind: 'baseline:reference',
          title: 'Reference',
          attrs: anyPack(
            BaselineAttrsSchema,
            create(BaselineAttrsSchema, {
              label: 'Reference',
              status: BaselineStatus.MEASURED,
              metric: 'throughput',
              unit: 'ops/s',
              direction: Direction.MAX,
              value: 50,
              samples: 2,
            }),
          ),
        },
      },
    },
  };
}
function capacityBody(): Body {
  return {
    case: 'span',
    value: {
      change: Change.ENDED,
      span: {
        id: 'capacity',
        runtimeType: RuntimeType.FLOW,
        name: 'Capacity',
        status: SpanStatus.DONE,
        startedAt: 0,
        endedAt: 2,
        label: {
          kind: 'capacity_wait',
          title: 'Capacity',
          attrs: anyPack(
            CapacityWaitAttrsSchema,
            create(CapacityWaitAttrsSchema, {
              provider: 'provider-a',
              condition: CapacityCondition.RATE_LIMITED,
              policy: CapacityPolicy.WAIT,
            }),
          ),
        },
      },
    },
  };
}
function adoptionBody(): Body {
  return {
    case: 'span',
    value: {
      change: Change.ENDED,
      span: {
        id: 'adoption',
        runtimeType: RuntimeType.FLOW,
        name: 'Adoption',
        status: SpanStatus.DONE,
        startedAt: 20,
        endedAt: 20,
        label: {
          kind: 'adoption',
          title: 'Adoption',
          attrs: anyPack(
            AdoptionAttrsSchema,
            create(AdoptionAttrsSchema, {candidate: 'revision-0', selection: Selection.CANDIDATE}),
          ),
          verdict: 'adopted',
        },
      },
    },
  };
}
export function storeFacts(): Body[] {
  return [...inputBodies(), comparisonBody(), baselineBody(), capacityBody(), adoptionBody()];
}
