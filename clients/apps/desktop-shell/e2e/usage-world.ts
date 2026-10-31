/**
 * The worlds the Usage page's screens run in: one machine with one project, whose usage the host
 * has cut into the buckets of the question (or could not, or does not offer to). Every figure is a
 * plain number the page words; nothing here names a real machine, path or person.
 */
import type {FakeBridgeSeed} from './fake-bridge.js';
import {FAKE_OFFER} from './fake-offer.js';
import {journeyFixture} from './journey-world.js';
import {PROJECT, SEED} from './world.js';

const HOST = 'ssh:host-b';

/** One series, as the Fake cuts it: each column's base value per bucket, ramped over the buckets. */
interface Pattern {
  readonly origin?: 'run' | 'outside';
  readonly provider: string;
  readonly model: string;
  readonly speed?: 'standard' | 'fast';
  readonly source?: 'model' | 'unpriced' | 'override' | 'recorded';
  readonly columns: Readonly<Record<string, number>>;
  /** Dollars per million tokens the list gives the model; absent: unpriced. */
  readonly rate?: {
    readonly input: number;
    readonly output: number;
    readonly cacheRead: number;
    readonly cacheWrite: number;
    readonly cacheWrite1h: null;
  };
}

/** What a host answers: the Fake reads this to cut the answer for each question. */
interface UsageHostSeed {
  readonly series: readonly Pattern[];
  readonly runs?: readonly {
    readonly project: number;
    readonly runId: string;
    readonly costs: readonly {
      readonly provider: string;
      readonly estimateUsd: number;
      readonly recordedUsd: number;
      readonly tokens: number;
    }[];
  }[];
  readonly agents?: readonly {
    readonly role: string;
    readonly model: string;
    readonly costs: readonly {
      readonly provider: string;
      readonly estimateUsd: number;
      readonly recordedUsd: number;
      readonly tokens: number;
    }[];
  }[];
  readonly recent?: readonly {
    readonly provider: string;
    readonly estimateUsd: number;
    readonly recordedUsd: number;
    readonly tokens: number;
  }[];
  readonly bests?: number;
  readonly pricing?: Readonly<Record<string, unknown>>;
}

const OPUS_RATE = {
  input: 5,
  output: 25,
  cacheRead: 0.5,
  cacheWrite: 6.25,
  cacheWrite1h: null,
} as const;
const SONNET_RATE = {
  input: 3,
  output: 15,
  cacheRead: 0.3,
  cacheWrite: 3.75,
  cacheWrite1h: null,
} as const;
const CODEX_RATE = {
  input: 1.25,
  output: 10,
  cacheRead: 0.125,
  cacheWrite: 1.25,
  cacheWrite1h: null,
} as const;

/** A series of model usage: `usd` dollars and `tokens` uncached tokens per bucket, with a cache. */
function model(
  provider: string,
  id: string,
  usd: number,
  rate: Pattern['rate'],
  extra: Partial<Pattern> = {},
): Pattern {
  return {
    provider,
    model: id,
    ...(rate === undefined ? {} : {rate}),
    columns: {
      uncached: 400_000,
      cached: 1_600_000,
      cacheWrite: 80_000,
      output: 120_000,
      turns: 6,
      estimateUsd: usd,
      recordedUsd: usd,
      inputUsd: usd * 0.3,
      cacheReadUsd: usd * 0.15,
      cacheWriteUsd: usd * 0.15,
      outputUsd: usd * 0.4,
      cacheSavingsUsd: usd * 0.9,
    },
    ...extra,
  };
}

const TASKS = [
  'mpmc',
  'spsc',
  'mpsc',
  'a task whose name is much longer than the others so the row has to keep its cost in view',
];

const ROLES = ['orchestrator', 'implementer', 'judge', 'profiler', 'merger', 'router'];
const MODELS: readonly (readonly [string, string])[] = [
  ['claude', 'claude-opus-4-5'],
  ['claude', 'claude-sonnet-4-5'],
  ['codex', 'gpt-5-codex'],
];

/** `count` listed runs, newest first. */
function runsOf(count: number) {
  return Array.from({length: count}, (_, at) => ({
    runId: `2026101${at % 10}-${String(100000 + at)}`,
    task: TASKS[at % TASKS.length] ?? null,
    createdAt: 1_760_000_000_000 - at * 60_000,
  }));
}

const COSTS = (usd: number) => [
  {provider: 'claude', estimateUsd: usd, recordedUsd: usd, tokens: Math.round(usd * 1_000_000)},
];

/** `count` runs whose costs fall from the first, and `agents` rows spread over them. */
function spending(count: number, agents: number): UsageHostSeed {
  const listed = runsOf(count);
  return {
    series: [
      model('claude', 'claude-opus-4-5', 30, OPUS_RATE),
      model('claude', 'claude-sonnet-4-5', 8, SONNET_RATE),
      model('codex', 'gpt-5-codex', 5, CODEX_RATE),
    ],
    runs: listed.map((run, at) => ({
      project: 0,
      runId: run.runId,
      costs: COSTS(Math.round((900 / (at + 1)) * 100) / 100),
    })),
    agents: Array.from({length: agents}, (_, at) => {
      const [, id] = MODELS[at % MODELS.length] ?? MODELS[0] ?? ['claude', 'claude-opus-4-5'];
      return {
        role: ROLES[at % ROLES.length] ?? 'implementer',
        model: id,
        costs: COSTS(Math.round((300 / (1 + at)) * 100) / 100),
      };
    }),
    recent: COSTS(12.5),
    bests: 2,
  };
}

/** What a world's host answers, or how it does not. */
type Answer = UsageHostSeed | 'unsupported' | 'failed';

export interface UsageCase {
  readonly name: string;
  readonly runs: number;
  readonly answer: Answer;
}

export const USAGE_CASES: readonly UsageCase[] = [
  {name: 'empty-period', runs: 3, answer: {series: []}},
  {name: 'one-run', runs: 1, answer: spending(1, 2)},
  {name: 'many-runs', runs: 40, answer: spending(40, 14)},
  {
    // Claude runs beside Codex, and a person's own use of both, outside any run.
    name: 'outside-runs',
    runs: 4,
    answer: {
      ...spending(4, 4),
      series: [
        model('claude', 'claude-opus-4-5', 30, OPUS_RATE),
        model('claude', 'claude-opus-4-5', 12, OPUS_RATE, {origin: 'outside'}),
        model('codex', 'gpt-5-codex', 5, CODEX_RATE),
      ],
    },
  },
  {
    name: 'unpriced-models',
    runs: 4,
    answer: {
      ...spending(4, 4),
      series: [
        model('claude', 'claude-opus-4-5', 30, OPUS_RATE),
        model('codex', 'gpt-next-mini', 0, undefined, {
          source: 'unpriced',
          columns: {uncached: 300_000, cached: 100_000, output: 50_000, turns: 4},
        }),
      ],
      pricing: {status: 'bundled', refreshDue: false},
    },
  },
  {
    name: 'large-figures',
    runs: 2,
    answer: {
      ...spending(2, 2),
      series: [model('claude', 'claude-opus-4-5', 3_456_789.5, OPUS_RATE)],
      recent: COSTS(98_765.43),
    },
  },
  {name: 'unsupported-host', runs: 2, answer: 'unsupported'},
  {name: 'failed-question', runs: 2, answer: 'failed'},
];

/** The Fake bridge's seed for `usage`: This project on one machine, answering as the case says. */
export function seedFor(usage: UsageCase): FakeBridgeSeed {
  return {
    ...SEED,
    theme: 'ink',
    projectRuns: {[`${HOST}|${PROJECT}`]: runsOf(usage.runs)},
    usage: {[HOST]: usage.answer},
    newRun: {...FAKE_OFFER, projects: {[PROJECT]: ['mpmc']}},
  };
}

/** The listed run reached from Usage uses the same native gateway as every run journey. */
export function runFixtureFor(usage: UsageCase) {
  const fixture = journeyFixture('empty');
  return {...fixture, runId: runsOf(usage.runs)[0]?.runId ?? fixture.runId};
}
