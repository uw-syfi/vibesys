import {create} from '@bufbuild/protobuf';
import {timestampFromMs} from '@bufbuild/protobuf/wkt';
import {describe, expect, it} from '@effect/vitest';
import {
  AgentRole,
  decodeValidated,
  encodeMessage,
  Harness,
  ModelCostSchema,
  ModelRefSchema,
  RoleCostSchema,
  type RunCost,
  RunCostSchema,
  SpendBucketSchema,
  UsageSchema,
  WatchRunCostResponseSchema,
} from '@vibesys/orchestrator-proto';
import fc from 'fast-check';
import {applyHostFrame, emptyHostModel} from '../host-model.js';
import {costViewOf} from './cost.js';
import {applyCost} from './fold.js';
import {emptyState} from './state.js';

const start = Date.parse('2026-11-01T09:00:00Z');
const optional = <T>(arb: fc.Arbitrary<T>) => fc.option(arb, {nil: undefined});
// Costs the host reports are finite and not negative; tokens are counts.
const dollars = fc.double({min: 0, max: 1e6, noNaN: true});
const tokens = optional(fc.bigInt({min: 0n, max: 10n ** 12n}));

const usage = fc
  .record({
    inputTokens: tokens,
    cacheReadTokens: tokens,
    cacheCreationTokens: tokens,
    outputTokens: tokens,
    reasoningTokens: tokens,
    costUsd: optional(dollars),
  })
  .map(fields => create(UsageSchema, fields));

const model = fc
  .record({harness: fc.constantFrom(Harness.CLAUDE_CODE, Harness.CODEX), model: fc.string()})
  .map(fields => create(ModelRefSchema, fields));

const unpriced = (turns: number) => fc.integer({min: 0, max: turns});

const roleCost = fc
  .tuple(
    fc.constantFrom(AgentRole.LEAD, AgentRole.PLANNER, AgentRole.WORKER, AgentRole.JUDGE),
    fc.nat({max: 50}),
    fc.nat({max: 500}).chain(turns => fc.tuple(fc.constant(turns), unpriced(turns))),
    usage,
  )
  .map(([role, agents, [turns, unpricedTurns], u]) =>
    create(RoleCostSchema, {role, agents, turns, unpricedTurns, usage: u}),
  );

const modelCost = fc
  .tuple(
    model,
    fc.nat({max: 500}).chain(turns => fc.tuple(fc.constant(turns), unpriced(turns))),
    usage,
  )
  .map(([m, [turns, unpricedTurns], u]) =>
    create(ModelCostSchema, {model: m, turns, unpricedTurns, usage: u}),
  );

const bucket = (index: number) =>
  fc
    .tuple(
      fc.double({min: 0, max: 1e5, noNaN: true}),
      fc.array(fc.tuple(model, fc.double({min: 1e-6, max: 1e5, noNaN: true})), {maxLength: 3}),
    )
    .map(([costUsd, spends]) =>
      create(SpendBucketSchema, {
        start: timestampFromMs(start + index * 3_600_000),
        costUsd,
        byModel: spends.map(([m, spend]) => ({model: m, costUsd: spend})),
      }),
    );

// Whole views as the contract allows them. The parts need not add up to the total: the client must
// show what the host sent and never work it out again, and a generator that kept them consistent
// would not tell a client that sums from one that does not.
const runCost = (runId: fc.Arbitrary<string>) =>
  fc
    .tuple(
      runId,
      fc.bigInt({min: 0n, max: 10n ** 6n}),
      usage,
      fc.nat({max: 1000}).chain(turns => fc.tuple(fc.constant(turns), unpriced(turns))),
      optional(fc.double({min: 1e-3, max: 1e6, noNaN: true})),
      fc.array(roleCost, {maxLength: 6}),
      fc.array(modelCost, {maxLength: 4}),
      fc
        .integer({min: 1, max: 48})
        .chain(n => fc.tuple(...Array.from({length: n}, (_, i) => bucket(i)))),
      optional(dollars),
    )
    .map(
      ([
        id,
        sequence,
        total,
        [turns, unpricedTurns],
        maxCostUsd,
        roles,
        models,
        buckets,
        lastHourUsd,
      ]) =>
        create(RunCostSchema, {
          runId: id,
          sequence,
          asOf: timestampFromMs(start + 10 * 3_600_000),
          total,
          maxCostUsd,
          turns,
          unpricedTurns,
          roles,
          models,
          series: {start: timestampFromMs(start), width: {seconds: 3600n, nanos: 0}, buckets},
          lastHourUsd,
        }),
    );

const runIds = fc.constantFrom('run-a', 'run-b', 'run-c');
const views = fc.array(runCost(runIds), {minLength: 1, maxLength: 12});

/** A view as it arrives: encoded and read back through the contract's own decoder. */
const arrives = (cost: RunCost): RunCost =>
  decodeValidated(RunCostSchema, encodeMessage(RunCostSchema, cost));

describe('a run cost view', () => {
  it('says what the host sent, number for number, and works out none of it', () => {
    fc.assert(
      fc.property(runCost(runIds), sent => {
        const got = costViewOf(arrives(sent));
        expect(got.total.costUsd).toBe(sent.total?.costUsd ?? null);
        expect(got.total.inputTokens).toBe(
          sent.total?.inputTokens === undefined ? null : Number(sent.total.inputTokens),
        );
        expect(got.maxCostUsd).toBe(sent.maxCostUsd ?? null);
        expect(got.lastHourUsd).toBe(sent.lastHourUsd ?? null);
        expect(got.turns).toBe(sent.turns);
        expect(got.unpricedTurns).toBe(sent.unpricedTurns);
        expect(
          got.roles.map(r => [r.role, r.agents, r.turns, r.unpricedTurns, r.usage.costUsd]),
        ).toEqual(
          sent.roles.map(r => [
            r.role,
            r.agents,
            r.turns,
            r.unpricedTurns,
            r.usage?.costUsd ?? null,
          ]),
        );
        expect(got.models.map(m => [m.model.harness, m.model.model, m.usage.costUsd])).toEqual(
          sent.models.map(m => [m.model?.harness, m.model?.model, m.usage?.costUsd ?? null]),
        );
        expect(got.series.buckets.map(b => [b.costUsd, b.byModel.map(s => s.costUsd)])).toEqual(
          (sent.series?.buckets ?? []).map(b => [b.costUsd, b.byModel.map(s => s.costUsd)]),
        );
        expect(got.series.bucketMs).toBe(3_600_000);
        expect(got.series.startMs).toBe(start);
        expect(got.series.buckets.map(b => b.startMs)).toEqual(
          (sent.series?.buckets ?? []).map((_, i) => start + i * 3_600_000),
        );
      }),
      {seed: 5},
    );
  });

  it('shows a price that was not reported as no price, never as zero', () => {
    const none = costViewOf(
      arrives(
        create(RunCostSchema, {
          runId: 'r',
          asOf: timestampFromMs(start),
          total: create(UsageSchema, {outputTokens: 5n}),
          turns: 1,
          unpricedTurns: 1,
          roles: [
            create(RoleCostSchema, {
              role: AgentRole.PROFILER,
              agents: 1,
              turns: 1,
              unpricedTurns: 1,
              usage: create(UsageSchema, {}),
            }),
          ],
          series: {
            start: timestampFromMs(start),
            width: {seconds: 60n, nanos: 0},
            buckets: [create(SpendBucketSchema, {start: timestampFromMs(start)})],
          },
        }),
      ),
    );
    expect(none.total.costUsd).toBeNull();
    expect(none.total.inputTokens).toBeNull();
    expect(none.maxCostUsd).toBeNull();
    expect(none.lastHourUsd).toBeNull();
    expect(none.roles[0]?.usage.costUsd).toBeNull();
  });

  it('keeps the last view of each run and leaves the rest of the state alone', () => {
    fc.assert(
      fc.property(views, sent => {
        const state = sent.map(arrives).reduce(applyCost, emptyState);
        for (const runId of ['run-a', 'run-b', 'run-c']) {
          const last = [...sent].reverse().find(cost => cost.runId === runId);
          expect(state.costs[runId]).toEqual(
            last === undefined ? undefined : costViewOf(arrives(last)),
          );
        }
        expect({...state, costs: {}}).toEqual(emptyState);
      }),
      {seed: 11},
    );
  });

  it('arrives through the host model as a frame of its own and moves no cursor', () => {
    fc.assert(
      fc.property(runCost(runIds), sent => {
        const model = applyHostFrame(emptyHostModel, {
          from: 'cost',
          response: create(WatchRunCostResponseSchema, {cost: arrives(sent)}),
        });
        expect(model.data.costs[sent.runId]).toEqual(costViewOf(arrives(sent)));
        expect(model.agentCursors).toEqual({});
        expect(model.data.sequence).toBe(0n);
      }),
      {seed: 13},
    );
  });
});
