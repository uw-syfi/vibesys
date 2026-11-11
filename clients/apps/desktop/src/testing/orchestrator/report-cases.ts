/**
 * Three runs the report has to hold in one layout, as scripted worlds for the fixture page and the
 * tests: a run that has just started (nothing measured, no idea, one agent), a run that failed
 * within a minute and a half with three measurements, and a run of six days with two objectives,
 * a few hundred measurements served (the host serves at most 400 points of a series and says it cut
 * the rest), dozens of ideas and many agents. Each is a function of nothing: the same bytes every time.
 */
import {create} from '@bufbuild/protobuf';
import {timestampFromMs} from '@bufbuild/protobuf/wkt';
import {
  AgentRole,
  AgentStatus,
  Direction,
  oneofIs,
  type RunChange,
  RunChangeSchema,
  RunStatus,
  RunStopReason,
  type SearchState,
} from '@vibesys/orchestrator-proto';
import type {CostView} from '../../state/index.js';
import {HOUR as COST_HOUR, START as COST_START, usage} from './cost-views.js';
import {measuredSearch, type ObjectiveSpec, withReport} from './report-fixtures.js';
import {generateScenario, type Scenario, START_MS} from './scenario.js';

/** The names a fixture address gives the three cases (`?case=started`). */
export const REPORT_CASES = ['started', 'failed', 'week'] as const;
export type ReportCase = (typeof REPORT_CASES)[number];

/** What a case serves: the run's changes, its search view and its cost. */
export interface ReportCaseWorld {
  readonly scenario: Scenario;
  readonly search: SearchState;
  readonly cost: CostView;
}

const DAY = 24 * COST_HOUR;
const MINUTE = 60_000;

/** Every kind of change a run's log carries, as `oneofIs` wants them named. */
const KINDS = {
  message: true,
  agent: true,
  run: true,
  turn: true,
  item: true,
  receipt: true,
  checkIn: true,
  runUpdated: true,
} as const;

/** The scenario with its run (and its lead) over at `atMs`, in the state and for the reason given. */
function withRunOver(
  scenario: Scenario,
  over: {readonly atMs: number; readonly status: RunStatus; readonly reason: RunStopReason},
): Scenario {
  const bodies = [...scenario.changes].reverse().map(change => change.body);
  const run = bodies.flatMap(body => (oneofIs(body, 'run', KINDS) ? [body.value] : []))[0];
  const lead = bodies.flatMap(body =>
    oneofIs(body, 'agent', KINDS) && body.value.role === AgentRole.LEAD ? [body.value] : [],
  )[0];
  if (run === undefined || lead === undefined) {
    throw new Error('the scenario has its run and its lead');
  }
  const at = timestampFromMs(over.atMs);
  const next = (body: RunChange['body'], sequence: number): RunChange =>
    create(RunChangeSchema, {
      sequence: BigInt(scenario.changes.length + sequence),
      runId: scenario.runId,
      at,
      body,
    });
  const failed = next({case: 'agent', value: {...lead, status: AgentStatus.FAILED}}, 1);
  const ended = next(
    {
      case: 'run',
      value: {...run, status: over.status, stopReason: over.reason, endedAt: at},
    },
    2,
  );
  return {...scenario, changes: [...scenario.changes, failed, ended], endMs: over.atMs};
}

const COSTED = {sequence: 1n} as const;

/** What a run that has just started has cost: the lead's first few turns, unpriced for none. */
const STARTED_COST: CostView = {
  runId: 'run',
  ...COSTED,
  asOfMs: START_MS,
  total: usage({inputTokens: 52_000, outputTokens: 2_100, costUsd: 0.04}),
  maxCostUsd: null,
  turns: 3,
  unpricedTurns: 0,
  roles: [
    {
      role: AgentRole.LEAD,
      agents: 1,
      turns: 3,
      unpricedTurns: 0,
      usage: usage({inputTokens: 52_000, outputTokens: 2_100, costUsd: 0.04}),
      totalTokens: 54_100,
    },
  ],
  models: [],
  series: {
    startMs: COST_START,
    bucketMs: 60_000,
    buckets: [{startMs: COST_START, costUsd: 0.04, byModel: []}],
  },
  lastHourUsd: 0.04,
  modelsTruncated: false,
};

/** A run that failed within a minute and a half: three agents' turns, one of them unpriced. */
const FAILED_COST: CostView = {
  ...STARTED_COST,
  total: usage({inputTokens: 190_000, outputTokens: 8_000, costUsd: 0.11}),
  turns: 10,
  unpricedTurns: 1,
  roles: [
    {
      role: AgentRole.LEAD,
      agents: 1,
      turns: 7,
      unpricedTurns: 0,
      usage: usage({inputTokens: 130_000, outputTokens: 5_000, costUsd: 0.09}),
      totalTokens: 135_000,
    },
    {
      role: AgentRole.JUDGE,
      agents: 1,
      turns: 1,
      unpricedTurns: 0,
      usage: usage({inputTokens: 14_000, outputTokens: 1_000, costUsd: 0.01}),
      totalTokens: 15_000,
    },
    {
      role: AgentRole.WORKER,
      agents: 1,
      turns: 2,
      unpricedTurns: 1,
      usage: usage({inputTokens: 46_000, outputTokens: 2_000}),
      totalTokens: 48_000,
    },
  ],
  lastHourUsd: null,
};

/** A run of six days: the lead, a judge and a crowd of workers, priced but for a few turns. */
const WEEK_COST: CostView = {
  ...STARTED_COST,
  asOfMs: COST_START + 6 * DAY,
  total: usage({
    inputTokens: 612_000_000,
    cacheReadTokens: 3_100_000_000,
    outputTokens: 41_000_000,
    costUsd: 412.8,
  }),
  maxCostUsd: 600,
  turns: 1284,
  unpricedTurns: 3,
  roles: [
    {
      role: AgentRole.WORKER,
      agents: 28,
      turns: 702,
      unpricedTurns: 0,
      usage: usage({outputTokens: 22_000_000, costUsd: 268.4}),
      totalTokens: 2_300_000_000,
    },
    {
      role: AgentRole.LEAD,
      agents: 1,
      turns: 412,
      unpricedTurns: 3,
      usage: usage({outputTokens: 14_000_000, costUsd: 96.2}),
      totalTokens: 1_100_000_000,
    },
    {
      role: AgentRole.JUDGE,
      agents: 12,
      turns: 170,
      unpricedTurns: 0,
      usage: usage({outputTokens: 5_000_000, costUsd: 48.2}),
      totalTokens: 310_000_000,
    },
  ],
  series: {
    startMs: COST_START,
    bucketMs: DAY,
    buckets: [60, 64, 71, 70, 75, 72.8].map((costUsd, day) => ({
      startMs: COST_START + day * DAY,
      costUsd,
      byModel: [],
    })),
  },
  lastHourUsd: 3.1,
};

/** The throughput and latency of a queue: no bound is declared for the latency. */
const QUEUE: readonly ObjectiveSpec[] = [
  {
    name: 'throughput',
    direction: Direction.MAXIMIZE,
    unit: 'ops/s',
    base: 48.6,
    best: 116.4,
    bound: 160,
  },
  {name: 'p99 latency', direction: Direction.MINIMIZE, unit: 'µs', base: 820, best: 331},
];

/** The world of a named case. */
export function reportCaseWorld(name: ReportCase): ReportCaseWorld {
  if (name === 'started') {
    const scenario = generateScenario({seed: 5, subagents: 0, live: true});
    return {
      scenario,
      search: measuredSearch(scenario.runId, {
        seed: 4,
        candidates: 0,
        startMs: START_MS,
        endMs: scenario.endMs,
        objectives: [],
      }),
      cost: STARTED_COST,
    };
  }
  if (name === 'failed') {
    const lived = generateScenario({seed: 5, subagents: 0, live: true});
    const scenario = withRunOver(lived, {
      atMs: lived.endMs + 5_000,
      status: RunStatus.FAILED,
      reason: RunStopReason.LEAD_GONE,
    });
    return {
      scenario,
      search: measuredSearch(scenario.runId, {
        seed: 4,
        candidates: 3,
        startMs: START_MS,
        endMs: scenario.endMs,
        objectives: [{name: 'score', direction: Direction.MAXIMIZE, unit: '', base: 10, best: 17}],
      }),
      cost: FAILED_COST,
    };
  }
  const lived = generateScenario({seed: 5, subagents: 27, live: true});
  const endMs = START_MS + 6 * DAY + 4 * COST_HOUR;
  const scenario = withReport(
    {...lived, endMs},
    {
      sequence: 9,
      atMs: endMs - 2 * COST_HOUR - 14 * MINUTE,
      headline:
        'Throughput is up 2.4 times on the input; the ring buffer and batching carry most of it. The latency tail is now the limit.',
      changed:
        'Kept #52 (relax the tail CAS) after 3 confirming measures; parked the allocator work.',
      inFlight: '6 implementers on backoff tuning and per-core queues.',
      decisions: 'Stop the allocator workstream unless p99 regresses.',
    },
  );
  return {
    scenario,
    search: measuredSearch(scenario.runId, {
      seed: 7,
      candidates: 3000,
      startMs: START_MS,
      endMs,
      objectives: QUEUE,
      ideas: 64,
    }),
    cost: WEEK_COST,
  };
}
