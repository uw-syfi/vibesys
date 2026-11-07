/**
 * A generator of any cost view the contract allows: a price absent anywhere, no turns, models of
 * one name on two harnesses, buckets of any width the host picks. The numbers are the test's:
 * nothing here works out a cost, because nothing in the app does.
 */
import {AgentRole, Harness} from '@vibesys/orchestrator-proto';
import fc from 'fast-check';
import type {CostModel, CostUsage, CostView} from '../../state/index.js';
import {HOUR, MODEL_LONG, START, usage} from './cost-views.js';

const widths = [60_000, 300_000, 900_000, 1_800_000, HOUR, 2 * HOUR, 6 * HOUR];
const money = fc.double({min: 0, max: 1e5, noNaN: true});
const optional = <T>(arbitrary: fc.Arbitrary<T>) => fc.option(arbitrary, {nil: null});
const tokens = optional(fc.integer({min: 0, max: 10 ** 12}));
const usageArb = fc
  .record({
    inputTokens: tokens,
    cacheReadTokens: tokens,
    cacheCreationTokens: tokens,
    outputTokens: tokens,
    reasoningTokens: tokens,
    costUsd: optional(money),
  })
  .map((fields): CostUsage => fields);
const modelArb = fc
  .record({
    harness: fc.constantFrom(Harness.CLAUDE_CODE, Harness.CODEX),
    model: fc.constantFrom('', 'model-a', 'model-b', MODEL_LONG.model),
  })
  .map((fields): CostModel => fields);
const turnsArb = fc
  .nat({max: 5000})
  .chain(turns => fc.record({turns: fc.constant(turns), unpricedTurns: fc.nat({max: turns})}));

const roleArb = fc.record({
  role: fc.constantFrom(AgentRole.LEAD, AgentRole.PLANNER, AgentRole.WORKER, AgentRole.JUDGE),
  agents: fc.nat({max: 60}),
  usage: usageArb,
  totalTokens: fc.nat({max: 10 ** 12}),
  counts: turnsArb,
});

/** Any cost view the contract allows, in the order a host sends them (cost order is not assumed). */
export const costViewArb: fc.Arbitrary<CostView> = fc
  .record({
    models: fc.uniqueArray(modelArb, {
      selector: model => `${model.harness}/${model.model}`,
      maxLength: 4,
    }),
    roles: fc.uniqueArray(roleArb, {selector: role => role.role, maxLength: 4}),
    counts: turnsArb,
    total: usageArb,
    maxCostUsd: optional(fc.double({min: 0.01, max: 1e5, noNaN: true})),
    bucketMs: fc.constantFrom(...widths),
    spends: fc.array(
      fc.array(fc.double({min: 1e-6, max: 1e4, noNaN: true}), {minLength: 4, maxLength: 4}),
      {minLength: 1, maxLength: 48},
    ),
    lastHourUsd: optional(money),
    modelTokens: fc.array(fc.nat({max: 10 ** 12}), {minLength: 4, maxLength: 4}),
    modelsTruncated: fc.boolean(),
  })
  .map(
    ({
      models,
      roles,
      counts,
      total,
      maxCostUsd,
      bucketMs,
      spends,
      lastHourUsd,
      modelTokens,
      modelsTruncated,
    }): CostView => {
      const costUsd = (at: number): number => spends[at]?.[0] ?? 0;
      return {
        runId: 'run-1',
        sequence: 1n,
        asOfMs: START + spends.length * bucketMs,
        total,
        maxCostUsd,
        turns: counts.turns,
        unpricedTurns: counts.unpricedTurns,
        roles: roles.map(({role, agents, usage: u, totalTokens, counts: c}) => ({
          role,
          agents,
          turns: c.turns,
          unpricedTurns: c.unpricedTurns,
          usage: u,
          // The host's number, drawn on its own: a screen shows it, so it need not match the kinds.
          totalTokens,
        })),
        models: models.map((model, at) => ({
          model,
          turns: counts.turns,
          unpricedTurns: counts.unpricedTurns,
          usage: usage({costUsd: costUsd(at)}),
          totalTokens: modelTokens[at] ?? 0,
        })),
        series: {
          startMs: START,
          bucketMs,
          buckets: spends.map((row, at) => ({
            startMs: START + at * bucketMs,
            costUsd: row.reduce((sum, value) => sum + value, 0),
            byModel: models.map((model, index) => ({model, costUsd: row[index] ?? 1e-6})),
          })),
        },
        lastHourUsd,
        modelsTruncated,
      };
    },
  );
