/**
 * A host's whole fleet as a scenario: many runs, some live and some over, each with the agents,
 * report, cost, budget and search facts a row of the Runs screen reads, and among the live ones a
 * run for each reason a run wants a look. A function of its options alone (seeded), so the same
 * options give the same bytes. The fixture page serves it, and the scale tests read its folded state.
 */
import {create} from '@bufbuild/protobuf';
import {timestampFromMs} from '@bufbuild/protobuf/wkt';
import {
  type Agent,
  AgentRole,
  AgentSchema,
  AgentStatus,
  AgentStopStage,
  BoundUnitCheck,
  Delivery,
  Direction,
  Harness,
  type Message,
  MessageSchema,
  ReportKind,
  ReportSchema,
  type Run,
  RunLimit,
  RunSchema,
  RunStatus,
  type SearchState,
  SearchStateSchema,
  UsageSchema,
} from '@vibesys/orchestrator-proto';
import {isLive} from '../../state/orchestrator/fleet.js';
import {ms} from '../../state/orchestrator/time.js';
import {fixtureTaskOf} from './builders.js';
import {fleetTurns} from './fleet-turns.js';
import {ChangeLog, Rng} from './log.js';
import {measuredSearch, type ObjectiveSpec} from './report-fixtures.js';
import type {Scenario} from './scenario.js';

export interface FleetOptions {
  readonly seed: number;
  /** Prefix of the run and agent ids, so two hosts never share an id by accident. */
  readonly prefix: string;
  /** How many runs, live and over together. */
  readonly runs: number;
  /** How many of them are live (the newest ones). */
  readonly live: number;
  /** The moment the fleet is read at: live runs began before it, over runs ended before it. */
  readonly nowMs: number;
  /** Give every nth run a model name too long for a cell, to see how it cuts it. */
  readonly longEvery?: number;
}

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;

const UNITS = ['tok/s', 'ops/s', 'TFLOPS', 'MB/s'] as const;

function searchOf(rng: Rng, runId: string, atMs: number): SearchState {
  const unit = rng.pick(UNITS);
  const bound = 100 + rng.int(900);
  const headValue = Math.round(bound * (0.2 + rng.next() * 0.85) * 100) / 100;
  const multi = rng.int(4) === 0;
  const served = rng.int(5) !== 0;
  return create(SearchStateSchema, {
    runId,
    revision: 1n,
    health: {state: {case: 'liveSince', value: timestampFromMs(atMs)}},
    head: {
      state: {
        case: 'current',
        value: {
          head: 'b'.repeat(40),
          result: {
            outcome: {
              case: 'measure',
              value: {
                metric: {name: 'throughput'},
                value: headValue,
                ...(multi
                  ? {
                      objectives: {metrics: [{name: 'throughput'}, {name: 'latency'}]},
                      observations: [{name: 'latency', value: 12 + rng.int(80)}],
                    }
                  : {}),
              },
            },
          },
        },
      },
    },
    bounds: served
      ? [
          {
            objective: 'throughput',
            direction: Direction.MAXIMIZE,
            versions: [
              {
                seq: 1n,
                objective: 'throughput',
                direction: Direction.MAXIMIZE,
                value: bound,
                unit,
                note: 'memory bandwidth',
                origin: {runId, agent: 'lead', at: timestampFromMs(atMs)},
              },
            ],
            headValue,
            percentOfBound: Math.round((headValue / bound) * 1000) / 10,
            measuredUnit: unit,
            unitCheck: BoundUnitCheck.MATCH,
          },
        ]
      : [],
  });
}

/**
 * The search of a run that has measured candidates, on one objective or (one run in four) two, as an
 * orchestrator that serves a series sends it.
 */
function measuredOf(rng: Rng, run: Run, nowMs: number): SearchState {
  const base = 50 + rng.int(100);
  const objectives: ObjectiveSpec[] = [
    {
      name: 'throughput',
      direction: Direction.MAXIMIZE,
      unit: rng.pick(UNITS),
      base,
      best: base * (1.5 + rng.next() * 2),
      bound: base * 5,
    },
    ...(rng.int(4) === 0
      ? [
          {
            name: 'latency',
            direction: Direction.MINIMIZE,
            unit: 'ms',
            base: 200,
            best: 90 + rng.int(50),
            bound: 60,
          },
        ]
      : []),
  ];
  return measuredSearch(run.id, {
    seed: 1 + rng.int(1_000_000),
    candidates: 3 + rng.int(40),
    startMs: ms(run.createdAt) ?? nowMs,
    endMs: ms(run.endedAt) ?? nowMs,
    objectives,
  });
}

/** When one run of the fleet began and ended, and how it stands: a function of its place in the fleet. */
interface Timing {
  readonly live: boolean;
  readonly startMs: number;
  readonly endMs: number | undefined;
  readonly status: RunStatus;
}

function timingOf(rng: Rng, options: FleetOptions, index: number): Timing {
  const live = index < options.live;
  const startMs = options.nowMs - (live ? (1 + rng.int(60)) * HOUR : (2 + index) * HOUR * 3);
  if (live) {
    const status = rng.int(8) === 0 ? RunStatus.STOPPING : RunStatus.RUNNING;
    return {live, startMs, endMs: undefined, status};
  }
  const status = rng.int(5) === 0 ? RunStatus.FAILED : RunStatus.ENDED;
  // A run that is over ended before the fleet is read, whatever its length.
  const endMs = Math.min(
    startMs + (1 + rng.int(40)) * HOUR,
    options.nowMs - (1 + rng.int(60)) * MINUTE,
  );
  return {live, startMs, endMs, status};
}

/**
 * Why a live run wants a look, by its place among the live runs, so a handful of them shows every
 * reason: it stood still, it reached its cost or its time limit, its lead posted a final report, or
 * jobs of it are stuck (its search says so). Most live runs want no look. A run that is over wants
 * none here: the checkouts it kept are paths on the host, which no fixture names.
 */
type LiveReason = 'stalled' | 'cost' | 'time' | 'final' | 'stuck' | null;

const LIVE_REASONS: readonly LiveReason[] = [
  'stalled',
  'cost',
  'final',
  'stuck',
  'time',
  null,
  null,
];

const reasonOf = (options: FleetOptions, index: number): LiveReason =>
  index < options.live ? (LIVE_REASONS[index % LIVE_REASONS.length] ?? null) : null;

/** The facts of the run that say why it wants a look; none for a run that wants none. */
function reasonFacts(reason: LiveReason, nowMs: number, startMs: number, cost: number) {
  switch (reason) {
    case 'stalled':
      return {stalledSince: timestampFromMs(Math.max(startMs, nowMs - 40 * MINUTE))};
    case 'cost': {
      const atMs = Math.max(startMs, nowMs - 20 * MINUTE);
      // The cost passed the limit by what was in flight when it was reached.
      const maxCostUsd = Math.max(1, Math.floor(cost / 50) * 50);
      return {limitReached: RunLimit.COST, limitReachedAt: timestampFromMs(atMs), maxCostUsd};
    }
    case 'time': {
      const atMs = Math.max(startMs, nowMs - 10 * MINUTE);
      return {
        limitReached: RunLimit.TIME,
        limitReachedAt: timestampFromMs(atMs),
        deadline: timestampFromMs(atMs),
      };
    }
    case 'final':
      return {finalReported: true};
    case 'stuck':
    case null:
      return {};
  }
}

function runOf(rng: Rng, options: FleetOptions, id: string, index: number): Run {
  const timing = timingOf(rng, options, index);
  const cost = Math.round(rng.next() * 1500 * 100) / 100;
  const {task, card} = fixtureTaskOf(
    index,
    options.longEvery !== undefined && index % options.longEvery === 0,
  );
  const reason = reasonOf(options, index);
  // Drawn in this order whatever the reason, so a reason changes no other run of the fleet.
  const priced = rng.int(10) !== 0 || reason === 'cost';
  const capped = rng.int(3) === 0;
  return create(RunSchema, {
    id,
    repository: 'decode-service',
    task,
    taskCard: card,
    status: timing.status,
    leadAgentId: `${id}-lead`,
    fanoutLimit: 4,
    createdAt: timestampFromMs(timing.startMs),
    ...(timing.endMs === undefined ? {} : {endedAt: timestampFromMs(timing.endMs)}),
    usage: create(UsageSchema, priced ? {costUsd: cost} : {}),
    ...(capped ? {maxCostUsd: Math.ceil(cost / 100 + 1) * 100} : {}),
    ...reasonFacts(reason, options.nowMs, timing.startMs, cost),
  });
}

/** The jobs a stuck run's search says are not finished and that nothing works on. */
function stuckOf(run: Run, search: SearchState | undefined, nowMs: number): SearchState {
  const {troubles} = create(SearchStateSchema, {
    troubles: [1, 2, 3].map(job => ({
      jobId: `${run.id}-job-${job}`,
      failure: {message: 'No worker has taken the job'},
    })),
  });
  return search === undefined
    ? create(SearchStateSchema, {
        runId: run.id,
        revision: 1n,
        health: {state: {case: 'liveSince', value: timestampFromMs(nowMs)}},
        troubles,
      })
    : {...search, troubles};
}

function agentStatusOf(rng: Rng, run: Run, lead: boolean): AgentStatus {
  if (!isLive(run.status)) return AgentStatus.ENDED;
  if (lead) return AgentStatus.WAITING;
  // Drawn for a stalled run too, so the rest of the fleet is the same; nothing works in one.
  const idle = rng.int(4) === 0 || run.stalledSince !== undefined;
  return idle ? AgentStatus.IDLE : AgentStatus.ACTIVE;
}

function agentOf(rng: Rng, run: Run, index: number): Agent {
  const lead = index === 0;
  const startMs = (ms(run.createdAt) ?? 0) + MINUTE;
  return create(AgentSchema, {
    id: `${run.id}-${lead ? 'lead' : `a${index}`}`,
    runId: run.id,
    parentAgentId: lead ? '' : run.leadAgentId,
    role: lead ? AgentRole.LEAD : AgentRole.WORKER,
    harness: Harness.CODEX,
    // The lead runs on the host's default model, which no price is known for; the workers on named ones.
    model: lead ? '' : index % 2 === 0 ? 'model-b' : 'model-a',
    status: agentStatusOf(rng, run, lead),
    session: 1,
    stopStage: AgentStopStage.NONE,
    startedAt: timestampFromMs(startMs),
  });
}

function reportOf(run: Run, atMs: number, kind: ReportKind): Message {
  return create(MessageSchema, {
    id: `${run.id}-report`,
    runId: run.id,
    fromAgentId: run.leadAgentId,
    delivery: Delivery.CONSUMED,
    at: timestampFromMs(atMs),
    body: {
      case: 'report',
      value: create(ReportSchema, {
        headline: kind === ReportKind.FINAL ? 'Done' : 'Progress',
        kind,
      }),
    },
  });
}

/** One run of the fleet with its agents, its report (most runs have one) and its search state. */
function pushRun(
  rng: Rng,
  options: FleetOptions,
  log: ChangeLog,
  searches: SearchState[],
  index: number,
): void {
  const run = runOf(rng, options, `${options.prefix}-run-${index}`, index);
  const startMs = ms(run.createdAt) ?? options.nowMs;
  log.push(startMs, {case: 'run', value: run});
  const agents = 1 + rng.int(5);
  const members = Array.from({length: agents}, (_, agent) => agentOf(rng, run, agent));
  for (const member of members) log.push(startMs + MINUTE, {case: 'agent', value: member});
  // The turns come from a stream of their own, so adding them moved nothing else of the fleet.
  for (const {at, turn} of fleetTurns(options.seed, run, members, options.nowMs))
    log.push(at, {case: 'turn', value: turn});
  // A run whose lead posted its final report has a report, whatever the draw; the draws are the
  // same either way, so the rest of the fleet is too.
  const final = run.finalReported;
  if (rng.int(4) !== 0) {
    const reportAt = (ms(run.endedAt) ?? options.nowMs) - (1 + rng.int(90)) * MINUTE;
    const kind = final ? ReportKind.FINAL : ReportKind.PROGRESS;
    log.push(reportAt, {case: 'message', value: reportOf(run, reportAt, kind)});
  } else if (final) {
    const reportAt = options.nowMs - 30 * MINUTE;
    log.push(reportAt, {case: 'message', value: reportOf(run, reportAt, ReportKind.FINAL)});
  }
  // Most runs have measured candidates; some have a head and a bound and no series (a host that
  // does not serve one, or a run with nothing measured yet), and some have no search at all.
  const kind = rng.int(6);
  const search =
    kind === 0
      ? undefined
      : kind === 1
        ? searchOf(rng, run.id, options.nowMs)
        : measuredOf(rng, run, options.nowMs);
  if (reasonOf(options, index) === 'stuck') searches.push(stuckOf(run, search, options.nowMs));
  else if (search !== undefined) searches.push(search);
}

export function fleetScenario(options: FleetOptions): Scenario & {
  readonly searches: readonly SearchState[];
} {
  const rng = new Rng(options.seed);
  const log = new ChangeLog(`${options.prefix}-run-0`);
  const searches: SearchState[] = [];
  for (let index = 0; index < options.runs; index++) pushRun(rng, options, log, searches, index);
  return {
    runId: `${options.prefix}-run-0`,
    changes: log.changes,
    plans: [],
    deltas: [],
    endMs: options.nowMs,
    searches,
  };
}
