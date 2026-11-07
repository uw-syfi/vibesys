/**
 * What the host serves a run report, as scripted facts: a search view with measured candidates on
 * one or more objectives and a declared bound, the lead's reports, and a cost view. The Fake works out
 * what the orchestrator's projection works out (the best-so-far marks, the frontier, the percent of
 * the bound) the plainest way, and the Go projection's own tests pin the real one, so a screen over
 * this reads served views and derives nothing itself.
 */
import {create} from '@bufbuild/protobuf';
import {timestampFromMs} from '@bufbuild/protobuf/wkt';
import {
  type AgentRole,
  Delivery,
  Direction,
  JobState,
  MessageSchema,
  ReportKind,
  RoleCostSchema,
  RunChangeSchema,
  type RunCost,
  RunCostSchema,
  type SearchState,
  SearchStateSchema,
  UnreadableReason,
  UsageSchema,
} from '@vibesys/orchestrator-proto';
import {Rng} from './log.js';
import type {Scenario} from './scenario.js';

export interface ObjectiveSpec {
  readonly name: string;
  readonly direction: Direction;
  readonly unit: string;
  /** What the first candidates measure, and what the last ones approach. */
  readonly base: number;
  readonly best: number;
  /** The speed-of-light bound the lead declares, when it declares one. */
  readonly bound?: number;
}

export interface MeasuredOptions {
  readonly seed: number;
  readonly candidates: number;
  readonly startMs: number;
  readonly endMs: number;
  readonly objectives: readonly ObjectiveSpec[];
  /** The lead said how it derived the bound, and revised it once. */
  readonly revisedBound?: boolean;
}

/** The two objectives of a serving task, with their declared bounds. */
export const SERVING: readonly ObjectiveSpec[] = [
  {
    name: 'throughput',
    direction: Direction.MAXIMIZE,
    unit: 'tok/s',
    base: 4800,
    best: 11_100,
    bound: 15_600,
  },
  {name: 'p99_latency', direction: Direction.MINIMIZE, unit: 'ms', base: 212, best: 118, bound: 74},
];

const RUN_AGENT = 'lead';

interface Candidate {
  readonly jobId: string;
  readonly commit: string;
  readonly atMs: number;
  readonly values: readonly number[];
}

function hex40(rng: Rng): string {
  return Array.from({length: 40}, () => rng.int(16).toString(16)).join('');
}

function candidatesOf(options: MeasuredOptions): readonly Candidate[] {
  const rng = new Rng(options.seed);
  return Array.from({length: options.candidates}, (_, index) => {
    const progress = (index + 1) / options.candidates;
    return {
      jobId: `job-${options.seed}-${index + 1}`,
      commit: hex40(rng),
      atMs: Math.round(
        options.startMs + (options.endMs - options.startMs) * Math.sqrt(progress) * 0.98,
      ),
      values: options.objectives.map(
        objective =>
          objective.base + (objective.best - objective.base) * progress * (0.7 + 0.3 * rng.next()),
      ),
    };
  });
}

const beats = (direction: Direction, a: number, b: number): boolean =>
  direction === Direction.MINIMIZE ? a < b : a > b;

/** The Fake's best-so-far marks: a point beats every earlier one. */
function seriesOf(options: MeasuredOptions, candidates: readonly Candidate[]) {
  return options.objectives.map((objective, at) => {
    let best: number | undefined;
    return {
      objective: objective.name,
      direction: objective.direction,
      points: candidates.map(candidate => {
        const value = candidate.values[at] ?? 0;
        const bestSoFar = best === undefined || beats(objective.direction, value, best);
        if (bestSoFar) best = value;
        return {
          jobId: candidate.jobId,
          commit: candidate.commit,
          at: timestampFromMs(candidate.atMs),
          value,
          bestSoFar,
        };
      }),
    };
  });
}

/** The Fake's frontier: no other candidate is at least as good on every objective and better on one. */
function tradeoffOf(options: MeasuredOptions, candidates: readonly Candidate[]) {
  const directions = options.objectives.map(objective => objective.direction);
  const dominates = (a: Candidate, b: Candidate): boolean =>
    directions.every((d, i) => !beats(d, b.values[i] ?? 0, a.values[i] ?? 0)) &&
    directions.some((d, i) => beats(d, a.values[i] ?? 0, b.values[i] ?? 0));
  return {
    objectives: options.objectives.map(({name, direction}) => ({name, direction})),
    points: candidates.map(candidate => ({
      jobId: candidate.jobId,
      commit: candidate.commit,
      at: timestampFromMs(candidate.atMs),
      values: [...candidate.values],
      onFrontier: !candidates.some(other => other !== candidate && dominates(other, candidate)),
    })),
  };
}

const percentOf = (direction: Direction, bound: number, value: number): number =>
  direction === Direction.MINIMIZE ? (bound / value) * 100 : (value / bound) * 100;

function measureOf(options: MeasuredOptions, candidate: Candidate) {
  const [scored, ...others] = options.objectives;
  return {
    case: 'measure' as const,
    value: {
      metric: {name: scored?.name ?? '', direction: scored?.direction ?? Direction.MAXIMIZE},
      value: candidate.values[0] ?? 0,
      observations: others.map((objective, at) => ({
        name: objective.name,
        values: [candidate.values[at + 1] ?? 0],
        value: candidate.values[at + 1] ?? 0,
      })),
      ...(others.length === 0
        ? {}
        : {
            objectives: {
              metrics: options.objectives.map(({name, direction}) => ({name, direction})),
            },
          }),
    },
  };
}

/**
 * The search view of a run that has measured `options.candidates` candidates between `startMs` and
 * `endMs` on its objectives, with the lead's bounds declared and the head at the best candidate on
 * the first objective. With no candidates it has no measurement, no series and no trade-off.
 */
export function measuredSearch(
  runId: string,
  options: MeasuredOptions,
  health: {readonly stale?: boolean} = {},
): SearchState {
  const candidates = candidatesOf(options);
  const [first] = options.objectives;
  const head = candidates.reduce<Candidate | undefined>(
    (best, candidate) =>
      best === undefined ||
      beats(first?.direction ?? Direction.MAXIMIZE, candidate.values[0] ?? 0, best.values[0] ?? 0)
        ? candidate
        : best,
    undefined,
  );
  const submitter = {runId, agent: RUN_AGENT, at: timestampFromMs(options.startMs)};
  return create(SearchStateSchema, {
    runId,
    revision: 1n,
    health: {
      state: health.stale
        ? {
            case: 'unreadable',
            value: {reason: UnreadableReason.STREAM_LOST, since: timestampFromMs(options.endMs)},
          }
        : {case: 'liveSince', value: timestampFromMs(options.startMs)},
    },
    jobs: candidates.map(candidate => ({
      id: candidate.jobId,
      state: JobState.SUCCEEDED,
      submittedAt: timestampFromMs(candidate.atMs - 60_000),
      submitters: [submitter],
    })),
    results: candidates.map(candidate => ({
      jobId: candidate.jobId,
      commit: candidate.commit,
      recordedAt: timestampFromMs(candidate.atMs),
      outcome: measureOf(options, candidate),
    })),
    ...(head === undefined
      ? {}
      : {
          head: {
            state: {
              case: 'current' as const,
              value: {
                head: head.commit,
                result: {
                  jobId: head.jobId,
                  commit: head.commit,
                  recordedAt: timestampFromMs(head.atMs),
                  outcome: measureOf(options, head),
                },
              },
            },
          },
        }),
    series: candidates.length === 0 ? [] : seriesOf(options, candidates),
    ...(candidates.length === 0 || options.objectives.length < 2
      ? {}
      : {tradeoff: tradeoffOf(options, candidates)}),
    bounds: options.objectives.flatMap((objective, at) => {
      const {bound} = objective;
      if (bound === undefined) return [];
      const value = (version: number): number =>
        version === 1 && options.revisedBound === true ? bound * 0.9 : bound;
      const headValue = head?.values[at];
      return [
        {
          objective: objective.name,
          direction: objective.direction,
          versions: (options.revisedBound === true ? [1, 2] : [2]).map(seq => ({
            seq: BigInt(seq + at * 2),
            objective: objective.name,
            direction: objective.direction,
            value: value(seq),
            unit: objective.unit,
            note: `Derived from the memory bandwidth over the bytes each ${objective.name} step moves.`,
            origin: {...submitter, at: timestampFromMs(options.startMs + 600_000 * seq)},
          })),
          ...(headValue === undefined
            ? {}
            : {
                headValue,
                percentOfBound: percentOf(objective.direction, bound, headValue),
              }),
        },
      ];
    }),
  });
}

/** The scenario with a report the lead posted at `atMs`, numbered `sequence`. */
export function withReport(
  scenario: Scenario,
  report: {
    readonly sequence: number;
    readonly atMs: number;
    readonly headline: string;
    readonly kind?: ReportKind;
    readonly changed?: string;
    readonly inFlight?: string;
    readonly decisions?: string;
    readonly openForUser?: string;
  },
): Scenario {
  const {atMs, sequence, kind, ...words} = report;
  const message = create(MessageSchema, {
    id: `msg-report-${sequence}`,
    runId: scenario.runId,
    at: timestampFromMs(atMs),
    delivery: Delivery.CONSUMED,
    body: {
      case: 'report',
      value: {
        kind: kind ?? ReportKind.PROGRESS,
        sequence,
        changed: '',
        inFlight: '',
        decisions: '',
        openForUser: '',
        ...words,
      },
    },
  });
  const change = create(RunChangeSchema, {
    sequence: BigInt(scenario.changes.length + 1),
    runId: scenario.runId,
    at: timestampFromMs(atMs),
    body: {case: 'message', value: message},
  });
  return {...scenario, changes: [...scenario.changes, change]};
}

export interface CostSpec {
  readonly asOfMs: number;
  readonly startMs: number;
  /** Null for a run no turn of which is priced. */
  readonly totalUsd: number | null;
  readonly limitUsd: number | null;
  readonly lastHourUsd: number | null;
  readonly unpricedTurns?: number;
  readonly types: readonly {
    readonly role: AgentRole;
    readonly agents: number;
    readonly costUsd: number | null;
  }[];
}

/** The cost view the host would send: every number is the test's, and the Fake adds up nothing. */
export function costFixture(runId: string, spec: CostSpec): RunCost {
  const unpriced = spec.unpricedTurns ?? 0;
  const turns = Math.max(unpriced, 12);
  return create(RunCostSchema, {
    runId,
    sequence: 3n,
    asOf: timestampFromMs(spec.asOfMs),
    total: create(UsageSchema, {
      outputTokens: 9000n,
      ...(spec.totalUsd === null ? {} : {costUsd: spec.totalUsd}),
    }),
    ...(spec.limitUsd === null ? {} : {maxCostUsd: spec.limitUsd}),
    turns,
    unpricedTurns: unpriced,
    roles: spec.types.map(type =>
      create(RoleCostSchema, {
        role: type.role,
        agents: type.agents,
        turns: type.agents,
        usage: create(UsageSchema, {
          outputTokens: 100n,
          ...(type.costUsd === null ? {} : {costUsd: type.costUsd}),
        }),
      }),
    ),
    series: {
      start: timestampFromMs(spec.startMs),
      width: {seconds: 3600n, nanos: 0},
      buckets: [{start: timestampFromMs(spec.startMs), costUsd: spec.totalUsd ?? 0}],
    },
    ...(spec.lastHourUsd === null ? {} : {lastHourUsd: spec.lastHourUsd}),
  });
}
