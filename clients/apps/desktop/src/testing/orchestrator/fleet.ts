/**
 * A host's whole fleet as a scenario: many runs, some live and some over, each with the agents,
 * report, cost, budget and search facts a row of the Runs screen reads. A function of its options
 * alone (seeded), so the same options give the same bytes. The fixture page serves it, and the
 * scale tests read its folded state.
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
  RunSchema,
  RunStatus,
  type SearchState,
  SearchStateSchema,
  UsageSchema,
} from '@vibesys/orchestrator-proto';
import {isLive} from '../../state/orchestrator/fleet.js';
import {ms} from '../../state/orchestrator/time.js';
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
  /** Name every nth run with a very long task name, to see how a cell cuts it. */
  readonly longEvery?: number;
}

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;

const NAMES = [
  'Qwen3 serving · MI300X',
  'KV cache kernel · H100',
  'queue-rs · mpmc',
  'Sparse MoE router · H100',
  'Flash attention backward · H100',
  'Tokenizer throughput · CPU',
  'GEMM tiling · MI300X',
  'Radix tree · rust',
  'Whisper decode · H100',
  'Paged attention · MI300X',
];

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

function nameOf(options: FleetOptions, index: number): string {
  const name = NAMES[index % NAMES.length] ?? 'run';
  const long = options.longEvery !== undefined && index % options.longEvery === 0;
  return long ? `${name} with a task name that runs on and on and on and on` : name;
}

function runOf(rng: Rng, options: FleetOptions, id: string, index: number): Run {
  const timing = timingOf(rng, options, index);
  const cost = Math.round(rng.next() * 1500 * 100) / 100;
  return create(RunSchema, {
    id,
    repository: 'decode-service',
    task: nameOf(options, index),
    status: timing.status,
    leadAgentId: `${id}-lead`,
    fanoutLimit: 4,
    createdAt: timestampFromMs(timing.startMs),
    ...(timing.endMs === undefined ? {} : {endedAt: timestampFromMs(timing.endMs)}),
    usage: create(UsageSchema, rng.int(10) === 0 ? {} : {costUsd: cost}),
    ...(rng.int(3) === 0 ? {maxCostUsd: Math.ceil(cost / 100 + 1) * 100} : {}),
  });
}

function agentStatusOf(rng: Rng, live: boolean, lead: boolean): AgentStatus {
  if (!live) return AgentStatus.ENDED;
  if (lead) return AgentStatus.WAITING;
  return rng.int(4) === 0 ? AgentStatus.IDLE : AgentStatus.ACTIVE;
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
    status: agentStatusOf(rng, isLive(run.status), lead),
    session: 1,
    stopStage: AgentStopStage.NONE,
    startedAt: timestampFromMs(startMs),
  });
}

function reportOf(run: Run, atMs: number): Message {
  return create(MessageSchema, {
    id: `${run.id}-report`,
    runId: run.id,
    fromAgentId: run.leadAgentId,
    delivery: Delivery.CONSUMED,
    at: timestampFromMs(atMs),
    body: {
      case: 'report',
      value: create(ReportSchema, {headline: 'Progress', kind: ReportKind.PROGRESS}),
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
  for (let agent = 0; agent < agents; agent++)
    log.push(startMs + MINUTE, {case: 'agent', value: agentOf(rng, run, agent)});
  if (rng.int(4) !== 0) {
    const reportAt = (ms(run.endedAt) ?? options.nowMs) - (1 + rng.int(90)) * MINUTE;
    log.push(reportAt, {case: 'message', value: reportOf(run, reportAt)});
  }
  // Most runs have measured candidates; some have a head and a bound and no series (a host that
  // does not serve one, or a run with nothing measured yet), and some have no search at all.
  const kind = rng.int(6);
  if (kind === 0) return;
  searches.push(
    kind === 1 ? searchOf(rng, run.id, options.nowMs) : measuredOf(rng, run, options.nowMs),
  );
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
