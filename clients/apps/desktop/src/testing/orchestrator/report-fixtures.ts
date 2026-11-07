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
  Delivery,
  Direction,
  IdeaVerdict,
  JobState,
  MessageSchema,
  PlanStatus,
  ReportKind,
  RunChangeSchema,
  type SearchState,
  SearchStateSchema,
  UnreadableReason,
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
  /** The lead's agents made this many plans, linked to the candidates, in a few workstreams. */
  readonly ideas?: number;
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

/** The workstreams of the Fake's ideas: the last has a title longer than any column. */
const WORKSTREAMS = [
  'GEMM and MoE',
  'Decode path',
  'Scheduler and the long, long name of a line of work that must be cut short',
] as const;

const IDEA_TITLES = [
  'hipGraph decode loop',
  'Fused expert dispatch',
  'A speculative draft reuse scheme whose title runs on well past any column a table could give it',
  'FP8 KV cache',
  'Split-K attention',
  'Persistent scheduler thread',
] as const;

const PLAN_STATUSES = [
  PlanStatus.WON,
  PlanStatus.REJECTED,
  PlanStatus.OPEN,
  PlanStatus.PARKED,
  PlanStatus.CHOSEN,
] as const;

const VERDICT_OF = {
  [PlanStatus.UNSPECIFIED]: IdeaVerdict.OPEN,
  [PlanStatus.OPEN]: IdeaVerdict.OPEN,
  [PlanStatus.CHOSEN]: IdeaVerdict.OPEN,
  [PlanStatus.WON]: IdeaVerdict.KEPT,
  [PlanStatus.REJECTED]: IdeaVerdict.REJECTED,
  [PlanStatus.PARKED]: IdeaVerdict.PARKED,
} as const satisfies Record<PlanStatus, IdeaVerdict>;

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

interface Gain {
  readonly metric: string;
  readonly ratio: number;
  readonly bestValue: number;
  readonly baselineValue: number;
}

/**
 * The Fake's ideas: `options.ideas` plans, each linked to the candidates that name it (the first
 * candidate, the baseline, and every seventh name none), the gains the Fake works out plainly (best
 * linked over the first candidate, oriented), and their counts by workstream.
 */
function ideasOf(options: MeasuredOptions, candidates: readonly Candidate[], runId: string) {
  const count = options.ideas ?? 0;
  const planOf = (index: number): number | undefined =>
    count === 0 || index === 0 || index % 7 === 6 ? undefined : (index % count) + 1;
  const plans = Array.from({length: count}, (_, at) => {
    const number = at + 1;
    const status = PLAN_STATUSES[at % PLAN_STATUSES.length] ?? PlanStatus.OPEN;
    const when = timestampFromMs(
      options.startMs + Math.round(((options.endMs - options.startMs) * number) / (count + 1)),
    );
    return {
      number,
      title: IDEA_TITLES[at % IDEA_TITLES.length] ?? '',
      status,
      createdAt: when,
      updatedAt: when,
      workstream: at % (WORKSTREAMS.length + 1),
      origin: {runId, agent: `impl-${(at % 3) + 1}`, at: when},
    };
  });
  const linked = (number: number): readonly Candidate[] =>
    candidates.filter((_, index) => planOf(index) === number);
  const baseline = candidates[0];
  const gainsOf = (members: readonly Candidate[]): Gain[] =>
    baseline === undefined
      ? []
      : options.objectives.flatMap((objective, at) => {
          const values = members.map(candidate => candidate.values[at] ?? 0);
          const best = values.reduce<number | undefined>(
            (top, value) =>
              top === undefined || beats(objective.direction, value, top) ? value : top,
            undefined,
          );
          const base = baseline.values[at] ?? 0;
          if (best === undefined || best <= 0 || base <= 0) return [];
          return [
            {
              metric: objective.name,
              ratio: objective.direction === Direction.MINIMIZE ? base / best : best / base,
              bestValue: best,
              baselineValue: base,
            },
          ];
        });
  const ideas = plans.map((plan, at) => ({
    planNumber: plan.number,
    verdict: VERDICT_OF[plan.status],
    candidateJobIds: linked(plan.number).map(candidate => candidate.jobId),
    best: gainsOf(linked(plan.number)),
    inRun: at % 5 !== 4,
  }));
  const numbers = [0, ...WORKSTREAMS.map((_, at) => at + 1)];
  const workstreamViews = numbers.flatMap(number => {
    const members = plans.filter(plan => plan.workstream === number);
    if (members.length === 0) return [];
    const of = (status: IdeaVerdict): number =>
      members.filter(plan => VERDICT_OF[plan.status] === status).length;
    const gains = options.objectives.flatMap(objective =>
      members
        .flatMap(plan => gainsOf(linked(plan.number)))
        .filter(gain => gain.metric === objective.name)
        .sort((a, b) => b.ratio - a.ratio)
        .slice(0, 1),
    );
    return [
      {
        number,
        kept: of(IdeaVerdict.KEPT),
        rejected: of(IdeaVerdict.REJECTED),
        open: of(IdeaVerdict.OPEN),
        parked: of(IdeaVerdict.PARKED),
        best: gains,
      },
    ];
  });
  return {
    plans,
    ideas,
    workstreamViews,
    workstreams: WORKSTREAMS.map((title, at) => ({
      number: at + 1,
      title,
      status: at + 1,
      note: 'paused: accuracy gate',
      origin: {runId, agent: 'lead', at: timestampFromMs(options.startMs)},
      updatedAt: timestampFromMs(options.startMs),
    })),
    jobPlans: candidates.map((_, index) => planOf(index)),
    unplanned: candidates.filter((_, index) => planOf(index) === undefined).length,
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
  const served = ideasOf(options, candidates, runId);
  return create(SearchStateSchema, {
    plans: served.plans,
    ideas: served.ideas,
    workstreams: served.workstreams,
    workstreamViews: served.workstreamViews,
    unplanned: served.unplanned,
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
    jobs: candidates.map((candidate, index) => ({
      id: candidate.jobId,
      plans: (() => {
        const plan = served.jobPlans[index];
        return plan === undefined ? [] : [plan];
      })(),
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
      const values = candidates.map(candidate => candidate.values[at] ?? 0);
      const bestValue = values.reduce<number | undefined>(
        (best, value) =>
          best === undefined || beats(objective.direction, value, best) ? value : best,
        undefined,
      );
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
          ...(bestValue === undefined
            ? {}
            : {bestValue, bestPercentOfBound: percentOf(objective.direction, bound, bestValue)}),
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
