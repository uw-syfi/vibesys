/**
 * A host's whole fleet as a scenario: many runs, some live and some over, each with the agents,
 * report, cost, budget and search facts a row of the Runs screen reads. A function of its options
 * alone (seeded), so the same options give the same bytes. The fixture page serves it, and the
 * scale tests read its folded state.
 */
import {create} from '@bufbuild/protobuf';
import {timestampFromMs} from '@bufbuild/protobuf/wkt';
import {
  AgentRole,
  AgentSchema,
  AgentStatus,
  AgentStopStage,
  Delivery,
  Direction,
  Harness,
  MessageSchema,
  ReportKind,
  ReportSchema,
  RunSchema,
  RunStatus,
  type SearchState,
  SearchStateSchema,
  UsageSchema,
} from '@vibesys/orchestrator-proto';
import {ChangeLog, Rng} from './log.js';
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
          },
        ]
      : [],
  });
}

export function fleetScenario(options: FleetOptions): Scenario & {
  readonly searches: readonly SearchState[];
} {
  const rng = new Rng(options.seed);
  const log = new ChangeLog(`${options.prefix}-run-0`);
  const searches: SearchState[] = [];
  for (let index = 0; index < options.runs; index++) {
    const id = `${options.prefix}-run-${index}`;
    const live = index < options.live;
    const startMs = options.nowMs - (live ? (1 + rng.int(60)) * HOUR : (2 + index) * HOUR * 3);
    const endMs = live ? undefined : startMs + (1 + rng.int(40)) * HOUR;
    const status = live
      ? rng.int(8) === 0
        ? RunStatus.STOPPING
        : RunStatus.RUNNING
      : rng.int(5) === 0
        ? RunStatus.FAILED
        : RunStatus.ENDED;
    const long = options.longEvery !== undefined && index % options.longEvery === 0;
    const name = long
      ? `${NAMES[index % NAMES.length]} with a task name that runs on and on and on and on`
      : (NAMES[index % NAMES.length] ?? 'run');
    const cost = Math.round(rng.next() * 1500 * 100) / 100;
    log.push(startMs, {
      case: 'run',
      value: create(RunSchema, {
        id,
        repository: 'decode-service',
        task: name,
        status,
        leadAgentId: `${id}-lead`,
        fanoutLimit: 4,
        createdAt: timestampFromMs(startMs),
        ...(endMs === undefined ? {} : {endedAt: timestampFromMs(endMs)}),
        usage: create(UsageSchema, rng.int(10) === 0 ? {} : {costUsd: cost}),
        ...(rng.int(3) === 0 ? {maxCostUsd: Math.ceil(cost / 100 + 1) * 100} : {}),
      }),
    });
    const agents = 1 + rng.int(5);
    for (let agent = 0; agent < agents; agent++) {
      log.push(startMs + MINUTE, {
        case: 'agent',
        value: create(AgentSchema, {
          id: `${id}-${agent === 0 ? 'lead' : `a${agent}`}`,
          runId: id,
          parentAgentId: agent === 0 ? '' : `${id}-lead`,
          role: agent === 0 ? AgentRole.LEAD : AgentRole.WORKER,
          harness: Harness.CODEX,
          status: !live
            ? AgentStatus.ENDED
            : agent === 0
              ? AgentStatus.WAITING
              : rng.int(4) === 0
                ? AgentStatus.IDLE
                : AgentStatus.ACTIVE,
          session: 1,
          stopStage: AgentStopStage.NONE,
          startedAt: timestampFromMs(startMs + MINUTE),
        }),
      });
    }
    if (rng.int(4) !== 0) {
      const reportAt = (endMs ?? options.nowMs) - (1 + rng.int(90)) * MINUTE;
      log.push(reportAt, {
        case: 'message',
        value: create(MessageSchema, {
          id: `${id}-report`,
          runId: id,
          fromAgentId: `${id}-lead`,
          delivery: Delivery.CONSUMED,
          at: timestampFromMs(reportAt),
          body: {
            case: 'report',
            value: create(ReportSchema, {headline: 'Progress', kind: ReportKind.PROGRESS}),
          },
        }),
      });
    }
    if (rng.int(6) !== 0) searches.push(searchOf(rng, id, options.nowMs));
  }
  return {
    runId: `${options.prefix}-run-0`,
    changes: log.changes,
    plans: [],
    deltas: [],
    endMs: options.nowMs,
    searches,
  };
}
