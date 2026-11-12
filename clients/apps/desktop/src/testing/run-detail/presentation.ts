/** Durable, deterministic generic run-detail scenarios for screen and presentation tests. */
import type {RunDetailDelivery} from '../../core/run-detail/index.js';
import type {RunDetailProjection} from '../../state/run-detail/index.js';

export const GENERIC_RUN_DETAIL_FIXTURES = [
  'live',
  'finished',
  'stale',
  'refused',
  'truncated',
  'long-name',
  'empty',
  'unknown-descriptor',
  'dynamic-role',
] as const;

export type GenericRunDetailFixture = (typeof GENERIC_RUN_DETAIL_FIXTURES)[number];

type Agent = RunDetailProjection['agents']['values'][number];
type Metric = RunDetailProjection['metrics']['values'][number];
type Workstream = RunDetailProjection['workstreams']['values'][number];
type Correctness = RunDetailProjection['correctness']['values'][number];
type ProjectionAvailability = RunDetailProjection['agents']['availability'];
type RunDetailFreshness = RunDetailProjection['freshness'];

const available = {kind: 'available'} as const satisfies ProjectionAvailability;
const runSource = {kind: 'run-log'} as const;
const current = (throughSequence: bigint): RunDetailFreshness => ({
  kind: 'current',
  throughSequence,
});
const recordSource = (sequence: bigint) => ({kind: 'run-record', sequence, epoch: 1}) as const;
const runScope = (runId: string) => ({kind: 'run', runId}) as const;
const spanScope = (runId: string, spanId: string) => ({kind: 'span', runId, spanId}) as const;

const section = <T>(
  runId: string,
  freshness: RunDetailFreshness,
  values: readonly T[],
  availability: ProjectionAvailability = available,
) => ({source: runSource, scope: runScope(runId), freshness, availability, values});

export interface RepresentativeRunDetailOptions {
  readonly runId?: string;
  /** Open strategy-selected role; no closed role catalogue is applied. */
  readonly dynamicRole?: string;
}

const representativeAgent = (runId: string, dynamicRole: string): Agent => ({
  spanId: 'agent-dynamic',
  parentSpanId: 'work-throughput',
  agentId: 'agent:dynamic',
  role: dynamicRole,
  stage: 'implementation',
  startedBy: ['orchestrator'],
  model: 'model-x',
  provider: 'provider-y',
  reasoningEffort: 'high',
  harnessVersion: '7',
  client: 'provider',
  providerSessionIds: ['session-open'],
  lifecycle: 'running',
  usage: {
    inputTokens: 11n,
    cacheReadTokens: 3n,
    cacheCreationTokens: undefined,
    outputTokens: 7n,
    reasoningTokens: 2n,
    costUsd: 0.25,
    durationMs: 1_500n,
  },
  source: recordSource(6n),
  scope: spanScope(runId, 'agent-dynamic'),
});

const representativeMetrics = (runId: string): readonly Metric[] => [
  {
    name: 'throughput',
    unit: 'items/s',
    direction: 'max',
    role: 'objective',
    source: recordSource(1n),
    scope: runScope(runId),
    points: [
      {value: 4_800, clock: 1, source: recordSource(2n), scope: runScope(runId)},
      {value: 7_200, clock: 2, source: recordSource(7n), scope: runScope(runId)},
    ],
    omittedPoints: 0,
  },
  {
    name: 'gpu_utilization',
    unit: 'percent',
    direction: 'none',
    role: 'resource',
    source: recordSource(1n),
    scope: runScope(runId),
    points: [{value: 75, clock: 2, source: recordSource(8n), scope: runScope(runId)}],
    omittedPoints: 0,
  },
];

const representativeWorkstream = (runId: string): Workstream => ({
  spanId: 'work-throughput',
  parentSpanId: undefined,
  title: 'Improve serving throughput',
  lifecycle: 'running',
  verdict: undefined,
  reason: undefined,
  links: [{rel: 'builds_on', to: 'revision-base'}],
  startedAt: 1_000,
  endedAt: undefined,
  spanFailure: undefined,
  hypothesisId: 'hypothesis-throughput',
  parentRevision: 'revision-base',
  baseRound: 1,
  round: 2,
  idea: 'Fuse the typed serving path',
  revision: 'revision-candidate',
  outcome: undefined,
  summary: 'The typed path is under measurement.',
  probe: false,
  workstreamFailure: undefined,
  files: [{path: 'src/serve.ts', diffOffset: 12n, diffLength: 48n}],
  attempts: 1,
  profileSkipped: false,
  judgeVerdict: undefined,
  repeatedError: false,
  sequence: 1,
  source: recordSource(4n),
  scope: spanScope(runId, 'work-throughput'),
});

const representativeCorrectness = (runId: string): Correctness => ({
  spanId: 'gate-throughput',
  parentSpanId: 'work-throughput',
  title: 'Candidate correctness',
  lifecycle: 'failed',
  verdict: 'failed_check',
  reason: 'accuracy',
  links: [{rel: 'candidate', to: 'work-throughput'}],
  startedAt: 1_100,
  endedAt: 1_300,
  spanFailure: undefined,
  stage: 'measurement',
  subject: 'candidate',
  recipe: 'trusted',
  command: 'evaluate',
  reused: false,
  failedCheck: {check: 'correctness', completed: 93n, required: 100n, unit: 'trials'},
  inputFailure: undefined,
  frameworkSource: 'gpu',
  sourceLabel: 'candidate-evaluation',
  stages: ['accuracy', 'benchmark'],
  source: recordSource(5n),
  scope: spanScope(runId, 'gate-throughput'),
});

/**
 * A representative bespoke-serving projection with only run-attributed resource metrics. The
 * correctness gate's `gpu` source is framework provenance and remains unrelated to metric scope.
 */
export function representativeRunDetailProjection(
  options: RepresentativeRunDetailOptions = {},
): RunDetailProjection {
  const runId = options.runId ?? 'serve-bespoke-01';
  const dynamicRole = options.dynamicRole ?? 'bespoke-serving-systems-critic';
  const freshness = current(9n);

  return {
    runId,
    freshness,
    truncation: {
      agentEvictions: 0,
      correctnessGateEvictions: 0,
      epochEvictions: 0,
      issueEvictions: 0,
      metricSeriesEvictions: 0,
      workstreamEvictions: 0,
    },
    lifecycle: {value: 'running', source: recordSource(1n), scope: runScope(runId)},
    agents: section(runId, freshness, [representativeAgent(runId, dynamicRole)]),
    metrics: section(runId, freshness, representativeMetrics(runId)),
    objectives: section(runId, freshness, [
      {
        series: 'throughput',
        direction: 'max',
        source: recordSource(1n),
        scope: runScope(runId),
      },
    ]),
    workstreams: section(runId, freshness, [representativeWorkstream(runId)]),
    correctness: section(runId, freshness, [representativeCorrectness(runId)]),
    unprojectedSpans: section(runId, freshness, []),
  };
}

const withFreshness = (
  projection: RunDetailProjection,
  freshness: RunDetailFreshness,
): RunDetailProjection => ({
  ...projection,
  freshness,
  agents: {...projection.agents, freshness},
  metrics: {...projection.metrics, freshness},
  objectives: {...projection.objectives, freshness},
  workstreams: {...projection.workstreams, freshness},
  correctness: {...projection.correctness, freshness},
  unprojectedSpans: {...projection.unprojectedSpans, freshness},
});

const finishedProjection = (runId: string): RunDetailProjection => {
  const freshness = current(20n);
  const projection = withFreshness(representativeRunDetailProjection({runId}), freshness);
  return {
    ...projection,
    lifecycle: {value: 'finished', source: recordSource(20n), scope: runScope(runId)},
    agents: {
      ...projection.agents,
      values: projection.agents.values.map(agent => ({...agent, lifecycle: 'done'})),
    },
    workstreams: {
      ...projection.workstreams,
      values: projection.workstreams.values.map(workstream => ({
        ...workstream,
        lifecycle: 'done',
        endedAt: 2_000,
        outcome: 'kept',
      })),
    },
    correctness: {
      ...projection.correctness,
      values: projection.correctness.values.map(gate => ({...gate, lifecycle: 'done'})),
    },
  };
};

const truncatedProjection = (runId: string): RunDetailProjection => {
  const projection = representativeRunDetailProjection({runId});
  const partial = {kind: 'partial', reasons: ['state-truncated']} as const;
  return {
    ...projection,
    truncation: {
      agentEvictions: 2,
      correctnessGateEvictions: 3,
      epochEvictions: 1,
      issueEvictions: 0,
      metricSeriesEvictions: 4,
      workstreamEvictions: 5,
    },
    agents: {...projection.agents, availability: partial},
    metrics: {
      ...projection.metrics,
      availability: partial,
      values: projection.metrics.values.map((metric, index) =>
        index === 0 ? {...metric, omittedPoints: 42} : metric,
      ),
    },
    objectives: {...projection.objectives, availability: partial},
    workstreams: {...projection.workstreams, availability: partial},
    correctness: {...projection.correctness, availability: partial},
  };
};

const longNameProjection = (runId: string): RunDetailProjection => {
  const projection = representativeRunDetailProjection({
    runId,
    dynamicRole:
      'reviewer-for-bespoke-serving-systems-with-an-intentionally-long-strategy-selected-role',
  });
  const long =
    'Investigate the persistent scheduler, decode graph, cache layout, and every interaction between them without hiding the end of this title';
  return {
    ...projection,
    agents: {
      ...projection.agents,
      values: projection.agents.values.map(agent => ({
        ...agent,
        model: 'provider/model-with-a-very-long-version-and-deployment-name-for-layout-testing',
        providerSessionIds: [
          'session-with-a-very-long-provider-owned-identity-that-must-never-be-used-as-layout',
        ],
      })),
    },
    workstreams: {
      ...projection.workstreams,
      values: projection.workstreams.values.map(workstream => ({
        ...workstream,
        title: long,
        idea: long,
      })),
    },
    correctness: {
      ...projection.correctness,
      values: projection.correctness.values.map(gate => ({...gate, title: long})),
    },
  };
};

const emptyProjection = (runId: string): RunDetailProjection => {
  const freshness = current(0n);
  return {
    runId,
    freshness,
    truncation: {
      agentEvictions: 0,
      correctnessGateEvictions: 0,
      epochEvictions: 0,
      issueEvictions: 0,
      metricSeriesEvictions: 0,
      workstreamEvictions: 0,
    },
    lifecycle: undefined,
    agents: section(runId, freshness, []),
    metrics: section(runId, freshness, []),
    objectives: section(runId, freshness, [], {
      kind: 'unavailable',
      reason: 'typed-fact-not-recorded',
    }),
    workstreams: section(runId, freshness, []),
    correctness: section(runId, freshness, []),
    unprojectedSpans: section(runId, freshness, []),
  };
};

const unknownDescriptorProjection = (runId: string): RunDetailProjection => {
  const projection = representativeRunDetailProjection({runId});
  return {
    ...projection,
    workstreams: {
      ...projection.workstreams,
      availability: {kind: 'partial', reasons: ['descriptor-unknown']},
      values: [],
    },
    unprojectedSpans: {
      ...projection.unprojectedSpans,
      values: [
        {
          spanId: 'unknown-workstream',
          kind: 'custom-workstream',
          attrsType: 'example.custom.v1.WorkstreamAttrs',
          declaredAttrsType: 'example.custom.v1.WorkstreamAttrs',
          reason: 'descriptor-unknown',
          source: recordSource(9n),
          scope: spanScope(runId, 'unknown-workstream'),
        },
      ],
    },
  };
};

/** A fresh delivery for each named screen state; no returned fixture shares mutable arrays. */
export function genericRunDetailFixture(
  fixture: GenericRunDetailFixture,
  runId = `fixture-${fixture}`,
): RunDetailDelivery {
  switch (fixture) {
    case 'live':
      return {_tag: 'Live', projection: representativeRunDetailProjection({runId})};
    case 'finished':
      return {_tag: 'Live', projection: finishedProjection(runId)};
    case 'stale':
      return {
        _tag: 'Stale',
        why: 'disconnected',
        projection: representativeRunDetailProjection({runId}),
      };
    case 'refused':
      return {
        _tag: 'Refused',
        fault: {
          kind: 'Unimplemented',
          detail: 'generic run detail is not served by this host',
          retryable: false,
        },
        projection: null,
      };
    case 'truncated':
      return {_tag: 'Live', projection: truncatedProjection(runId)};
    case 'long-name':
      return {_tag: 'Live', projection: longNameProjection(runId)};
    case 'empty':
      return {_tag: 'Live', projection: emptyProjection(runId)};
    case 'unknown-descriptor':
      return {_tag: 'Live', projection: unknownDescriptorProjection(runId)};
    case 'dynamic-role':
      return {
        _tag: 'Live',
        projection: representativeRunDetailProjection({
          runId,
          dynamicRole: 'bespoke-serving-systems-critic',
        }),
      };
  }
}
