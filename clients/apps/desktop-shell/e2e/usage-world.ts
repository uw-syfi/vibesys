/**
 * The worlds the Usage page's screens run in: one machine with one project, whose runs the helper
 * has summed for the window (or could not, or does not offer to). Every figure is a plain number
 * the page words; nothing here names a real machine, path or person.
 */
import type {FakeBridgeSeed} from './fake-bridge.js';
import {FAKE_OFFER} from './fake-offer.js';
import {PROJECT, SEED} from './world.js';

const HOST = 'ssh:host-b';
const KEY = `${HOST}|${PROJECT}`;

interface Entry {
  readonly since: number;
  readonly until: number;
  readonly spentUsd: number;
  readonly spendPerHourUsd: number;
  readonly tokens: Record<string, number | null>;
  readonly turns: number;
  readonly unpricedTurns: number;
  readonly unpricedItemised: boolean;
  readonly untimedTurns: number;
  readonly newBests: number;
  readonly costPerNewBestUsd: number | null;
  readonly byRun: readonly {
    runId: string;
    task: string | null;
    costUsd: number;
    unpricedTurns: number;
  }[];
  readonly byAgent: readonly {
    role: string | null;
    harness: string | null;
    model: string | null;
    costUsd: number;
    turns: number;
    unpricedTurns: number;
  }[];
  readonly agentsOmitted: number;
  readonly missingRuns: readonly string[];
}

const NO_TOKENS = {input: null, cache_read: null, cache_write: null, output: null, reasoning: null};

const BASE: Entry = {
  since: 0,
  until: 0,
  spentUsd: 0,
  spendPerHourUsd: 0,
  tokens: NO_TOKENS,
  turns: 0,
  unpricedTurns: 0,
  unpricedItemised: true,
  untimedTurns: 0,
  newBests: 0,
  costPerNewBestUsd: null,
  byRun: [],
  byAgent: [],
  agentsOmitted: 0,
  missingRuns: [],
};

const ROLES = ['orchestrator', 'implementer', 'judge', 'profiler', 'merger', 'router'];
const MODELS: readonly (readonly [string, string])[] = [
  ['claude', 'claude-opus-4-5'],
  ['claude', 'claude-sonnet-4-5'],
  ['codex', 'gpt-5-codex'],
  ['codex', 'gpt-6.1-luna'],
];
const TASKS = [
  'mpmc',
  'spsc',
  'mpsc',
  'a task whose name is much longer than the others so the row has to keep its cost in view',
];

function runsOf(count: number) {
  return Array.from({length: count}, (_, at) => ({
    runId: `2026101${at % 10}-${String(100000 + at)}`,
    task: TASKS[at % TASKS.length] ?? null,
    createdAt: 1_760_000_000_000 - at * 60_000,
  }));
}

/** `count` runs whose costs fall from the first, with agents spread over them. */
function spending(count: number, agents: number): Entry {
  const listed = runsOf(count);
  const byRun = listed.map((run, at) => ({
    runId: run.runId,
    task: run.task,
    costUsd: Math.round((900 / (at + 1)) * 100) / 100,
    unpricedTurns: 0,
  }));
  const spent = byRun.reduce((total, run) => total + run.costUsd, 0);
  const byAgent = Array.from({length: agents}, (_, at) => {
    const [harness, model] = MODELS[at % MODELS.length] ??
      MODELS[0] ?? ['claude', 'claude-opus-4-5'];
    return {
      role: ROLES[at % ROLES.length] ?? null,
      harness,
      model,
      costUsd: Math.round((spent / agents / (1 + at / 3)) * 100) / 100,
      turns: 3 + at,
      unpricedTurns: 0,
    };
  });
  return {
    ...BASE,
    spentUsd: Math.round(spent * 100) / 100,
    spendPerHourUsd: 12.5,
    tokens: {
      input: 48_200_000,
      cache_read: 40_000_000,
      cache_write: null,
      output: 1_950_000,
      reasoning: null,
    },
    turns: byAgent.reduce((total, agent) => total + agent.turns, 0),
    newBests: 7,
    costPerNewBestUsd: spent / 7,
    byRun,
    byAgent,
  };
}

/** `entry` with `counts` of its first runs' and agents' turns unpriced (each count within the agent's turns). */
function unpricedOn(entry: Entry, counts: readonly number[]): Entry {
  return {
    ...entry,
    byRun: entry.byRun.map((run, at) => ({...run, unpricedTurns: counts[at] ?? 0})),
    byAgent: entry.byAgent.map((agent, at) => ({...agent, unpricedTurns: counts[at] ?? 0})),
  };
}

/** What a world's helper answers for the project, or how it does not. */
type Answer = Entry | 'unsupported' | 'failed';

export interface UsageCase {
  readonly name: string;
  readonly runs: number;
  readonly answer: Answer;
}

export const USAGE_CASES: readonly UsageCase[] = [
  {name: 'empty-window', runs: 3, answer: BASE},
  {name: 'one-run', runs: 1, answer: spending(1, 2)},
  {name: 'many-runs', runs: 40, answer: {...spending(40, 14), agentsOmitted: 7}},
  {
    name: 'unpriced-turns',
    runs: 6,
    answer: {
      ...unpricedOn(spending(6, 4), [3, 2]),
      unpricedTurns: 5,
      untimedTurns: 2,
      missingRuns: ['20261001-000001'],
      newBests: 0,
      costPerNewBestUsd: null,
      tokens: {
        input: 1_200_000,
        cache_read: null,
        cache_write: null,
        output: null,
        reasoning: null,
      },
    },
  },
  {
    // A helper too old to send per-run counts: the count has no rows to explain it.
    name: 'unpriced-no-breakdown',
    runs: 2,
    answer: {
      ...spending(2, 2),
      byRun: spending(2, 2).byRun.map(run => ({...run, unpricedTurns: 0})),
      unpricedTurns: 2,
      unpricedItemised: false,
    },
  },
  {
    name: 'large-figures',
    runs: 2,
    answer: {
      ...spending(2, 2),
      spentUsd: 12_345_678.9,
      spendPerHourUsd: 98_765.43,
      tokens: {
        input: 987_600_000_000,
        cache_read: null,
        cache_write: null,
        output: null,
        reasoning: null,
      },
      newBests: 1,
      costPerNewBestUsd: 12_345_678.9,
    },
  },
  {name: 'unsupported-helper', runs: 2, answer: 'unsupported'},
  {name: 'failed-question', runs: 2, answer: 'failed'},
];

/** The Fake bridge's seed for `usage`: This project on one machine, answering as the case says. */
export function seedFor(usage: UsageCase): FakeBridgeSeed {
  return {
    ...SEED,
    theme: 'ink',
    projectRuns: {[KEY]: runsOf(usage.runs)},
    usage: {[KEY]: usage.answer},
    newRun: {...FAKE_OFFER, projects: {[PROJECT]: ['mpmc']}},
  };
}
