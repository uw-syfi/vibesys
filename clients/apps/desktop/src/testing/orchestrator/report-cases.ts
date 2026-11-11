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
  type Agent,
  AgentRole,
  AgentSchema,
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

export {FAILED_COST, STARTED_COST, WEEK_COST};

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

/**
 * The scenario with one more agent of each role named, each a child of the lead that has already
 * ended its session: the agents a cost view that lists their types has to be about.
 */
function withEndedAgents(scenario: Scenario, roles: readonly AgentRole[]): Scenario {
  const lead = scenario.changes
    .map(change => change.body)
    .flatMap(body =>
      oneofIs(body, 'agent', KINDS) && body.value.role === AgentRole.LEAD ? [body.value] : [],
    )[0];
  if (lead === undefined) throw new Error('the scenario has its lead');
  const added = roles.map((role, at): RunChange => {
    const agent: Agent = create(AgentSchema, {
      ...lead,
      id: `${lead.id}-${at + 1}`,
      role,
      parentAgentId: lead.id,
      status: AgentStatus.ENDED,
    });
    return create(RunChangeSchema, {
      sequence: BigInt(scenario.changes.length + at + 1),
      runId: scenario.runId,
      at: timestampFromMs(scenario.endMs - 30_000),
      body: {case: 'agent', value: agent},
    });
  });
  return {...scenario, changes: [...scenario.changes, ...added]};
}

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

/**
 * A run that failed within a minute and a half: the lead's seven turns and the judge's one are
 * priced, the worker's one turn reported no price (so its type has no cost, and the run's is a floor).
 */
const FAILED_COST: CostView = {
  ...STARTED_COST,
  total: usage({inputTokens: 190_000, outputTokens: 8_000, costUsd: 0.1}),
  turns: 9,
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
      turns: 1,
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
  // The agents of the case's scenario by type (see `generateScenario` with 27 subagents), so the
  // table of the report and its facts line count the same twenty-eight agents.
  roles: [
    {
      role: AgentRole.WORKER,
      agents: 23,
      turns: 680,
      unpricedTurns: 0,
      usage: usage({
        inputTokens: 480_000_000,
        cacheReadTokens: 2_300_000_000,
        outputTokens: 21_000_000,
        costUsd: 255.4,
      }),
      totalTokens: 2_801_000_000,
    },
    {
      role: AgentRole.LEAD,
      agents: 1,
      turns: 412,
      unpricedTurns: 3,
      usage: usage({
        inputTokens: 110_000_000,
        cacheReadTokens: 640_000_000,
        outputTokens: 14_000_000,
        costUsd: 96.2,
      }),
      totalTokens: 764_000_000,
    },
    {
      role: AgentRole.JUDGE,
      agents: 1,
      turns: 170,
      unpricedTurns: 0,
      usage: usage({
        inputTokens: 14_000_000,
        cacheReadTokens: 60_000_000,
        outputTokens: 5_000_000,
        costUsd: 48.2,
      }),
      totalTokens: 79_000_000,
    },
    {
      role: AgentRole.PLANNER,
      agents: 2,
      turns: 12,
      unpricedTurns: 0,
      usage: usage({
        inputTokens: 4_000_000,
        cacheReadTokens: 20_000_000,
        outputTokens: 400_000,
        costUsd: 6,
      }),
      totalTokens: 24_400_000,
    },
    {
      role: AgentRole.PROFILER,
      agents: 1,
      turns: 10,
      unpricedTurns: 0,
      usage: usage({
        inputTokens: 4_000_000,
        cacheReadTokens: 80_000_000,
        outputTokens: 600_000,
        costUsd: 7,
      }),
      totalTokens: 84_600_000,
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

/** A report no screen can show whole: a headline of several sentences, paragraphs, and one unbroken path. */
const LONG_HEADLINE =
  'Throughput plateaued at 2.4 times the input for the last 18 hours: every idea in the backoff workstream measured within noise of the head, the per-core queue prototype regressed p99 by 30 percent under contention, and the allocator work is parked, so I am redirecting effort to the consumer wake-up path, which the profiler now shows as 41 percent of the cycles.';
const LONG_CHANGED =
  'Kept #52 (relax the tail CAS) after three confirming measures on the 16-producer configuration. Rejected #53, #55 and #57: each improved throughput by under one percent, inside the noise of the bench, and two of them raised p99 latency. Parked #54 (slab allocator) because the profile no longer shows allocation in the top ten. Re-ran the input twice to confirm the baseline has not drifted; it has not, within 0.4 percent.\n\nSplit the queue workstream into "wake-up path" and "per-core queues" so the two can be judged apart. The profile of the last build is in /srv/builds/mpmc-queue/target/release/deps/libmpmc_queue-4f8a2c91d0e3b7a5c61e09b2d7f35a8e.rlib/profile/perf-record-2026-11-06T22-56-12.data.';
const LONG_RUNNING =
  'Six implementers: two on futex-based wake-ups, two on batching wake-ups per 32 items, one on a per-core queue variant that avoids the cross-core steal, and one re-measuring #52 on the 64-producer configuration to make sure the gain holds at the highest contention level we test.';
const LONG_DECIDED =
  'Stop the allocator workstream unless p99 regresses. Treat any gain under 1.5 percent as noise until the bench variance is reduced. Do not pursue lock-free MPMC rewrites; the effort is out of proportion to the measured headroom.';

/** Plan titles longer than any column: a sentence, and a sentence with a path in it. */
const LONG_TITLES = [
  'Replace the spin-then-park consumer wake-up with a futex wait on the head index, batching the wake-ups per 32 items and falling back to the spin only under contention',
  'Pin each producer to a core and give it a private slot array in /srv/builds/mpmc-queue/target/release/deps/libmpmc_queue-4f8a2c91d0e3b7a5c61e09b2d7f35a8e.rlib so the tail CAS never crosses a socket',
];

/** The ids of the workers of a scenario, in the order they started: who makes the plans. */
const workersOf = (scenario: Scenario): readonly string[] => [
  ...new Set(
    scenario.changes.flatMap(({body}) =>
      oneofIs(body, 'agent', KINDS) && body.value.role === AgentRole.WORKER ? [body.value.id] : [],
    ),
  ),
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
    const generated = generateScenario({seed: 5, subagents: 0, live: true});
    // A short report with a field the lead left empty (what changed): the block leaves it out.
    const lived = withReport(withEndedAgents(generated, [AgentRole.JUDGE, AgentRole.WORKER]), {
      sequence: 1,
      atMs: generated.endMs - 10_000,
      headline: 'Measured the input; planning the first ideas.',
      inFlight: 'Profiler on the hot path.',
    });
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
  const lived = generateScenario({seed: 5, subagents: 27, live: true, tree: true});
  const endMs = START_MS + 6 * DAY + 4 * COST_HOUR;
  const scenario = withReport(
    {...lived, endMs},
    {
      sequence: 9,
      atMs: endMs - 2 * COST_HOUR - 14 * MINUTE,
      headline: LONG_HEADLINE,
      changed: LONG_CHANGED,
      inFlight: LONG_RUNNING,
      decisions: LONG_DECIDED,
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
      spread: true,
      agents: workersOf(scenario),
      extraTitles: LONG_TITLES,
    }),
    cost: WEEK_COST,
  };
}
