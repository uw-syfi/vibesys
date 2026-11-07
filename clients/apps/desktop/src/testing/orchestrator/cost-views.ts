/**
 * Cost views as the host serves them, for tests and the fixture page: named views for the cases a
 * screen has to get right (`cost-arbitrary.ts` generates any view the contract allows). The numbers are the test's: nothing here works out a
 * cost, because nothing in the app does.
 */
import {AgentRole, Harness} from '@vibesys/orchestrator-proto';
import type {CostModel, CostUsage, CostView} from '../../state/index.js';

export const START = Date.parse('2026-11-01T09:00:00Z');
export const HOUR = 3_600_000;

/** What a model is called when no one has named it: not a real model id. */
const MODEL_A: CostModel = {harness: Harness.CLAUDE_CODE, model: 'model-a'};
const MODEL_B: CostModel = {harness: Harness.CODEX, model: 'model-b'};
const MODEL_DEFAULT: CostModel = {harness: Harness.CODEX, model: ''};
export const MODEL_LONG: CostModel = {
  harness: Harness.CLAUDE_CODE,
  model: 'model-with-an-identifier-so-long-that-no-column-of-a-screen-holds-it-whole-v2',
};

const none: CostUsage = {
  inputTokens: null,
  cacheReadTokens: null,
  cacheCreationTokens: null,
  outputTokens: null,
  reasoningTokens: null,
  costUsd: null,
};

export const usage = (fields: Partial<CostUsage>): CostUsage => ({...none, ...fields});

const bucket = (at: number, spends: readonly (readonly [CostModel, number])[]) => ({
  startMs: START + at * HOUR,
  costUsd: spends.reduce((total, [, usd]) => total + usd, 0),
  byModel: spends.map(([model, costUsd]) => ({model, costUsd})),
});

/** A run in progress with priced and unpriced turns, two models and a cost limit. */
export const PRICED_AND_UNPRICED: CostView = {
  runId: 'run-1',
  sequence: 120n,
  asOfMs: START + 6 * HOUR,
  total: usage({
    inputTokens: 41_800_000,
    cacheReadTokens: 212_400_000,
    cacheCreationTokens: 9_600_000,
    outputTokens: 3_900_000,
    reasoningTokens: 5_200_000,
    costUsd: 318.4,
  }),
  maxCostUsd: 500,
  turns: 1433,
  unpricedTurns: 41,
  roles: [
    {
      role: AgentRole.WORKER,
      agents: 17,
      turns: 774,
      unpricedTurns: 0,
      usage: usage({outputTokens: 2_000_000, costUsd: 205}),
      totalTokens: 188_300_000,
    },
    {
      role: AgentRole.LEAD,
      agents: 1,
      turns: 436,
      unpricedTurns: 12,
      usage: usage({outputTokens: 1_500_000, costUsd: 66}),
      totalTokens: 61_000_000,
    },
    {
      role: AgentRole.JUDGE,
      agents: 1,
      turns: 33,
      unpricedTurns: 0,
      usage: usage({outputTokens: 100_000, costUsd: 5}),
      totalTokens: 1_950_000,
    },
    {
      role: AgentRole.PROFILER,
      agents: 1,
      turns: 41,
      unpricedTurns: 41,
      usage: usage({inputTokens: 2_200_000}),
      totalTokens: 2_200_000,
    },
  ],
  models: [
    {
      model: MODEL_A,
      turns: 1100,
      unpricedTurns: 0,
      usage: usage({costUsd: 206}),
      totalTokens: 190_400_000,
    },
    {
      model: MODEL_B,
      turns: 250,
      unpricedTurns: 0,
      usage: usage({costUsd: 88}),
      totalTokens: 52_100_000,
    },
    {
      model: MODEL_DEFAULT,
      turns: 83,
      unpricedTurns: 41,
      usage: usage({costUsd: 24.4}),
      totalTokens: 22_500_000,
    },
  ],
  series: {
    startMs: START,
    bucketMs: HOUR,
    buckets: [
      bucket(0, [[MODEL_A, 30]]),
      bucket(1, [
        [MODEL_A, 38],
        [MODEL_B, 12],
      ]),
      bucket(2, [
        [MODEL_B, 40],
        [MODEL_A, 50],
        [MODEL_DEFAULT, 4],
      ]),
      bucket(3, [[MODEL_A, 44]]),
      bucket(4, [
        [MODEL_A, 44],
        [MODEL_B, 36],
      ]),
      bucket(5, [[MODEL_DEFAULT, 20.4]]),
    ],
  },
  lastHourUsd: 14.2,
  modelsTruncated: false,
};

/** The same run once it has ended: nothing unpriced, and the host's clock stopped at its end. */
export const ENDED_RUN: CostView = {
  ...PRICED_AND_UNPRICED,
  unpricedTurns: 0,
  maxCostUsd: null,
  roles: PRICED_AND_UNPRICED.roles.slice(0, 3).map(role => ({...role, unpricedTurns: 0})),
  models: PRICED_AND_UNPRICED.models.map(model => ({...model, unpricedTurns: 0})),
};

/** Tokens were reported and no price ever was: every price is absent, none is zero. */
export const NO_PRICES: CostView = {
  runId: 'run-1',
  sequence: 12n,
  asOfMs: START + 2 * HOUR,
  total: usage({inputTokens: 90_000, outputTokens: 4_000}),
  maxCostUsd: 50,
  turns: 9,
  unpricedTurns: 9,
  roles: [
    {
      role: AgentRole.LEAD,
      agents: 1,
      turns: 9,
      unpricedTurns: 9,
      usage: usage({inputTokens: 90_000, outputTokens: 4_000}),
      totalTokens: 94_000,
    },
  ],
  models: [
    {
      model: MODEL_A,
      turns: 9,
      unpricedTurns: 9,
      usage: usage({inputTokens: 90_000, outputTokens: 4_000}),
      totalTokens: 94_000,
    },
  ],
  series: {startMs: START, bucketMs: HOUR, buckets: [bucket(0, []), bucket(1, [])]},
  lastHourUsd: null,
  modelsTruncated: false,
};

/** A run with no finished turn: there is nothing to split, and no price. */
export const NO_TURNS: CostView = {
  runId: 'run-1',
  sequence: 1n,
  asOfMs: START,
  total: usage({}),
  maxCostUsd: null,
  turns: 0,
  unpricedTurns: 0,
  roles: [],
  models: [],
  series: {startMs: START, bucketMs: 60_000, buckets: [bucket(0, [])]},
  lastHourUsd: null,
  modelsTruncated: false,
};

/** Names no column can hold, and a model that shares its name with another on a second harness. */
export const LONG_NAMES: CostView = {
  ...PRICED_AND_UNPRICED,
  models: [
    {
      model: MODEL_LONG,
      turns: 1000,
      unpricedTurns: 0,
      usage: usage({costUsd: 250}),
      totalTokens: 140_000_000,
    },
    {
      model: MODEL_A,
      turns: 400,
      unpricedTurns: 0,
      usage: usage({costUsd: 68.4}),
      totalTokens: 90_000_000,
    },
  ],
  series: {
    ...PRICED_AND_UNPRICED.series,
    buckets: [bucket(0, [[MODEL_LONG, 250]]), bucket(1, [[MODEL_A, 68.4]])],
  },
};
