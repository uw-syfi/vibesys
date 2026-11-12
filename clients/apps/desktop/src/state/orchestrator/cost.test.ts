import {create, type MessageInitShape} from '@bufbuild/protobuf';
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
  RunSchema,
  RunsSnapshotSchema,
  SpendBucketSchema,
  UsageSchema,
} from '@vibesys/orchestrator-proto';
import fc from 'fast-check';
import {applyHostFrame, emptyHostModel} from '../host-model.js';
import {readCost} from './cost.js';
import {applyCost, applySnapshot} from './fold.js';
import {emptyState} from './state.js';

/** The view a message reads as; a test that expects one fails with the field the read names. */
function viewOf(cost: RunCost) {
  const read = readCost(cost);
  if (read._tag === 'Missing') throw new Error(`the message lacks ${read.field}`);
  return read.view;
}

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
        const got = viewOf(arrives(sent));
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
    const none = viewOf(
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

  it('keeps the newest view of each run, the later arrival of equals, and the rest of the state alone', () => {
    fc.assert(
      fc.property(views, sent => {
        const state = sent.map(arrives).map(viewOf).reduce(applyCost, emptyState);
        for (const runId of ['run-a', 'run-b', 'run-c']) {
          const mine = sent.filter(cost => cost.runId === runId);
          const newest = mine.reduce<RunCost | undefined>(
            (best, cost) => (best === undefined || cost.sequence >= best.sequence ? cost : best),
            undefined,
          );
          expect(state.costs[runId]).toEqual(
            newest === undefined ? undefined : viewOf(arrives(newest)),
          );
        }
        expect({...state, costs: {}}).toEqual(emptyState);
      }),
      {seed: 11},
    );
  });

  it('is not replaced by an older view, whatever order the views arrive in', () => {
    fc.assert(
      fc.property(runCost(fc.constant('run-a')), fc.bigInt({min: 1n, max: 1000n}), (cost, back) => {
        const older = viewOf(arrives(cost));
        const newer = viewOf(arrives({...cost, sequence: cost.sequence + back}));
        const held = applyCost(emptyState, newer);
        // The older frame arriving late changes nothing, and not even the object.
        expect(applyCost(held, older)).toBe(held);
        // Arriving in the other order, the newer one still wins.
        expect(applyCost(applyCost(emptyState, older), newer).costs['run-a']).toEqual(newer);
      }),
      {seed: 17},
    );
  });

  it('is not replaced by a view of the same sequence worked out at an earlier instant', () => {
    fc.assert(
      fc.property(runCost(fc.constant('run-a')), cost => {
        const now = viewOf(arrives(cost));
        const earlier = {...now, asOfMs: now.asOfMs - 1000, lastHourUsd: 1};
        const held = applyCost(emptyState, now);
        expect(applyCost(held, earlier)).toBe(held);
        // The same sequence at a later instant is the newer view: the rate moves with the clock.
        const later = {...now, asOfMs: now.asOfMs + 1000, lastHourUsd: 2};
        expect(applyCost(held, later).costs['run-a']).toEqual(later);
      }),
      {seed: 19},
    );
  });

  it('is dropped with its run when the run list no longer has the run', () => {
    fc.assert(
      fc.property(
        views,
        fc.subarray(['run-a', 'run-b', 'run-c']),
        fc.bigInt({min: 0n, max: 10n}),
        (sent, kept, sequence) => {
          const held = sent.map(arrives).map(viewOf).reduce(applyCost, emptyState);
          const snapshot = create(RunsSnapshotSchema, {
            sequence,
            runs: kept.map(id => create(RunSchema, {id})),
          });
          const after = applySnapshot(held, snapshot);
          // Only the runs the list has keep a view, and those views are the ones held before.
          expect(Object.keys(after.costs).sort()).toEqual(
            Object.keys(held.costs)
              .filter(id => kept.includes(id))
              .sort(),
          );
          for (const [id, view] of Object.entries(after.costs)) expect(view).toBe(held.costs[id]);
        },
      ),
      {seed: 23},
    );
  });

  it('is not a view when a field the contract requires is absent, and names the field', () => {
    const whole = (fields: Partial<Exclude<MessageInitShape<typeof RunCostSchema>, RunCost>>) =>
      create(RunCostSchema, {
        runId: 'r',
        asOf: timestampFromMs(start),
        total: create(UsageSchema, {}),
        series: {
          start: timestampFromMs(start),
          width: {seconds: 60n, nanos: 0},
          buckets: [create(SpendBucketSchema, {start: timestampFromMs(start)})],
        },
        ...fields,
      });
    expect(readCost(whole({}))._tag).toBe('View');
    const model = {harness: Harness.CODEX, model: 'model-a'};
    const cases: readonly (readonly [string, RunCost])[] = [
      ['as_of', whole({asOf: undefined})],
      ['total', whole({total: undefined})],
      ['series', whole({series: undefined})],
      ['series.start', whole({series: {width: {seconds: 60n, nanos: 0}, buckets: []}})],
      ['series.width', whole({series: {start: timestampFromMs(start), buckets: []}})],
      [
        'series.buckets.start',
        whole({
          series: {
            start: timestampFromMs(start),
            width: {seconds: 60n, nanos: 0},
            buckets: [create(SpendBucketSchema, {})],
          },
        }),
      ],
      [
        'series.buckets.by_model.model',
        whole({
          series: {
            start: timestampFromMs(start),
            width: {seconds: 60n, nanos: 0},
            buckets: [
              create(SpendBucketSchema, {
                start: timestampFromMs(start),
                byModel: [{costUsd: 1}],
              }),
            ],
          },
        }),
      ],
      ['roles.usage', whole({roles: [create(RoleCostSchema, {role: AgentRole.LEAD, agents: 1})]})],
      [
        'models.model',
        whole({models: [create(ModelCostSchema, {usage: create(UsageSchema, {})})]}),
      ],
      ['models.usage', whole({models: [create(ModelCostSchema, {model})]})],
    ];
    for (const [field, message] of cases) {
      expect(readCost(message), field).toEqual({_tag: 'Missing', field});
    }
  });

  it('arrives through the host model as a frame of its own and moves no cursor', () => {
    fc.assert(
      fc.property(runCost(runIds), sent => {
        const model = applyHostFrame(emptyHostModel, {
          from: 'cost',
          phase: 'update',
          view: viewOf(arrives(sent)),
        });
        expect(model.data.costs[sent.runId]).toEqual(viewOf(arrives(sent)));
        expect(model.agentCursors).toEqual({});
        expect(model.data.sequence).toBe(0n);
      }),
      {seed: 13},
    );
  });

  it('lets a new stream replace a held view from a later sequence', () => {
    fc.assert(
      fc.property(
        runCost(fc.constant('run-a')),
        fc.bigInt({min: 1n, max: 1000n}),
        (cost, ahead) => {
          const initial = viewOf(arrives(cost));
          const beforeRestart = viewOf(arrives({...cost, sequence: cost.sequence + ahead}));
          const held = applyHostFrame(emptyHostModel, {
            from: 'cost',
            phase: 'update',
            view: beforeRestart,
          });
          const restarted = applyHostFrame(held, {
            from: 'cost',
            phase: 'initial',
            view: initial,
          });
          expect(restarted.data.costs['run-a']).toEqual(initial);
        },
      ),
      {seed: 29},
    );
  });
});
