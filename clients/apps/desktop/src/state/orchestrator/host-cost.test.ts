import {create} from '@bufbuild/protobuf';
import {timestampFromMs} from '@bufbuild/protobuf/wkt';
import {describe, expect, it} from '@effect/vitest';
import {
  AgentRole,
  decodeValidated,
  encodeMessage,
  Harness,
  type HostCost,
  HostCostSchema,
  HostRunCostSchema,
  ModelCostSchema,
  ModelRefSchema,
  OtherRunsCostSchema,
  RoleCostSchema,
  RunStatus,
  SpendBucketSchema,
  UsageSchema,
} from '@vibesys/orchestrator-proto';
import fc from 'fast-check';
import {applyHostFrame, emptyHostModel} from '../host-model.js';
import {applyHostCost} from './fold.js';
import {
  COST_DAY_MS,
  type HostCostWindow,
  lastDayWindow,
  readHostCost,
  sinceWindow,
  windowSinceMs,
} from './host-cost.js';
import {emptyState} from './state.js';

/** The view a message reads as; a test that expects one fails with the field the read names. */
function viewOf(cost: HostCost) {
  const read = readHostCost(cost);
  if (read._tag === 'Missing') throw new Error(`the message lacks ${read.field}`);
  return read.view;
}

const start = Date.parse('2026-11-01T09:00:00Z');
const optional = <T>(arb: fc.Arbitrary<T>) => fc.option(arb, {nil: undefined});
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

const counts = fc
  .nat({max: 500})
  .chain(turns => fc.record({turns: fc.constant(turns), unpricedTurns: fc.nat({max: turns})}));

const model = fc
  .record({harness: fc.constantFrom(Harness.CLAUDE_CODE, Harness.CODEX), model: fc.string()})
  .map(fields => create(ModelRefSchema, fields));

const run = fc
  .record({
    runId: fc.constantFrom('run-a', 'run-b', 'run-c', 'run-d'),
    name: fc.string({minLength: 1, maxLength: 40}),
    status: fc.constantFrom(
      RunStatus.RUNNING,
      RunStatus.ENDED,
      RunStatus.FAILED,
      RunStatus.STOPPING,
    ),
    maxCostUsd: optional(fc.double({min: 1e-3, max: 1e6, noNaN: true})),
    counts,
    usage,
    totalTokens: fc.bigInt({min: 0n, max: 10n ** 12n}),
  })
  .map(({counts: c, ...fields}) => create(HostRunCostSchema, {...fields, ...c}));

const others = fc
  .record({
    runs: fc.integer({min: 1, max: 500}),
    counts,
    usage,
    totalTokens: fc.bigInt({min: 0n, max: 10n ** 12n}),
  })
  .map(({counts: c, ...fields}) => create(OtherRunsCostSchema, {...fields, ...c}));

const roleCost = fc
  .record({
    role: fc.constantFrom(AgentRole.LEAD, AgentRole.WORKER, AgentRole.JUDGE),
    agents: fc.nat({max: 50}),
    counts,
    usage,
  })
  .map(({counts: c, ...fields}) => create(RoleCostSchema, {...fields, ...c}));

const modelCost = fc
  .record({model, counts, usage})
  .map(({counts: c, ...fields}) => create(ModelCostSchema, {...fields, ...c}));

const bucket = (index: number) =>
  fc
    .tuple(
      dollars,
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
const hostCost = fc
  .record({
    sequence: fc.bigInt({min: 0n, max: 10n ** 6n}),
    total: usage,
    counts,
    runs: fc.array(run, {maxLength: 5}),
    others: optional(others),
    roles: fc.array(roleCost, {maxLength: 4}),
    models: fc.array(modelCost, {maxLength: 4}),
    buckets: fc
      .integer({min: 1, max: 12})
      .chain(n => fc.tuple(...Array.from({length: n}, (_, i) => bucket(i)))),
    lastHourUsd: optional(dollars),
    modelsTruncated: fc.boolean(),
  })
  .map(({counts: c, others: rest, buckets, ...fields}) =>
    create(HostCostSchema, {
      ...fields,
      ...c,
      otherRuns: rest,
      runCount: fields.runs.length + (rest?.runs ?? 0),
      asOf: timestampFromMs(start + 10 * 3_600_000),
      series: {start: timestampFromMs(start), width: {seconds: 3600n, nanos: 0}, buckets},
    }),
  );

/** A view as it arrives: encoded and read back through the contract's own decoder. */
const arrives = (cost: HostCost): HostCost =>
  decodeValidated(HostCostSchema, encodeMessage(HostCostSchema, cost));

const windows: HostCostWindow[] = ['all', sinceWindow(start), sinceWindow(start + 3_600_000)];

describe('a host cost view', () => {
  it('says what the host sent, number for number, and works out none of it', () => {
    fc.assert(
      fc.property(hostCost, sent => {
        const got = viewOf(arrives(sent));
        expect(got.total.costUsd).toBe(sent.total?.costUsd ?? null);
        expect(got.runCount).toBe(sent.runCount);
        expect(got.turns).toBe(sent.turns);
        expect(got.unpricedTurns).toBe(sent.unpricedTurns);
        expect(got.lastHourUsd).toBe(sent.lastHourUsd ?? null);
        expect(got.modelsTruncated).toBe(sent.modelsTruncated);
        // The runs are in the order the host ranked them, each with its own figures.
        expect(
          got.runs.map(r => [
            r.runId,
            r.name,
            r.status,
            r.maxCostUsd,
            r.turns,
            r.unpricedTurns,
            r.usage.costUsd,
            r.totalTokens,
          ]),
        ).toEqual(
          sent.runs.map(r => [
            r.runId,
            r.name,
            r.status,
            r.maxCostUsd ?? null,
            r.turns,
            r.unpricedTurns,
            r.usage?.costUsd ?? null,
            Number(r.totalTokens),
          ]),
        );
        expect(got.otherRuns?.runs).toBe(sent.otherRuns?.runs);
        expect(got.otherRuns?.usage.costUsd ?? null).toBe(sent.otherRuns?.usage?.costUsd ?? null);
        expect(got.roles.map(r => [r.role, r.agents, r.usage.costUsd])).toEqual(
          sent.roles.map(r => [r.role, r.agents, r.usage?.costUsd ?? null]),
        );
        expect(got.models.map(m => [m.model.harness, m.model.model, m.usage.costUsd])).toEqual(
          sent.models.map(m => [m.model?.harness, m.model?.model, m.usage?.costUsd ?? null]),
        );
        expect(
          got.series.buckets.map(b => [b.startMs, b.costUsd, b.byModel.map(s => s.costUsd)]),
        ).toEqual(
          (sent.series?.buckets ?? []).map((b, i) => [
            start + i * 3_600_000,
            b.costUsd,
            b.byModel.map(s => s.costUsd),
          ]),
        );
        expect(got.series.bucketMs).toBe(3_600_000);
        expect(got.series.startMs).toBe(start);
      }),
      {seed: 5},
    );
  });

  it('shows a run or a total with no reported price as no price, never as zero', () => {
    const none = viewOf(
      arrives(
        create(HostCostSchema, {
          asOf: timestampFromMs(start),
          total: create(UsageSchema, {outputTokens: 5n}),
          runCount: 1,
          turns: 1,
          unpricedTurns: 1,
          runs: [
            create(HostRunCostSchema, {
              runId: 'run-a',
              name: 'task',
              status: RunStatus.RUNNING,
              turns: 1,
              unpricedTurns: 1,
              usage: create(UsageSchema, {outputTokens: 5n}),
              totalTokens: 5n,
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
    expect(none.lastHourUsd).toBeNull();
    expect(none.otherRuns).toBeNull();
    expect(none.runs[0]?.usage.costUsd).toBeNull();
    expect(none.runs[0]?.maxCostUsd).toBeNull();
  });

  it('names the field a message lacks, and reads no view from it', () => {
    fc.assert(
      fc.property(hostCost, fc.constantFrom('series', 'asOf', 'total'), (sent, field) => {
        const broken = create(HostCostSchema, {...arrives(sent), [field]: undefined});
        const read = readHostCost(broken);
        const named = {series: 'series', asOf: 'as_of', total: 'total'}[field];
        expect(read).toEqual({_tag: 'Missing', field: named});
      }),
      {seed: 3},
    );
  });
});

describe('the windows of a host cost view', () => {
  it('are told apart and read back to the instant they begin at', () => {
    expect(windowSinceMs('all')).toBeNull();
    fc.assert(
      fc.property(fc.integer({min: 1, max: 4_000_000_000_000}), ms => {
        const window = sinceWindow(ms);
        expect(windowSinceMs(window)).toBe(ms);
        expect(sinceWindow(ms + 1)).not.toBe(window);
      }),
    );
    expect(sinceWindow(1500.9)).toBe('since:1500');
  });

  it('opens the last-day window at exactly 24 hours before the supplied instant', () => {
    fc.assert(
      fc.property(fc.integer({min: COST_DAY_MS + 1, max: 4_000_000_000_000}), openedAtMs => {
        expect(windowSinceMs(lastDayWindow(openedAtMs))).toBe(openedAtMs - COST_DAY_MS);
      }),
    );
  });

  it('keep the newest view of each window apart, the later arrival of equals, and the rest of the state alone', () => {
    fc.assert(
      fc.property(
        fc.array(fc.tuple(fc.constantFrom(...windows), hostCost), {minLength: 1, maxLength: 12}),
        sent => {
          const state = sent
            .map(([window, cost]) => [window, viewOf(arrives(cost))] as const)
            .reduce((held, [window, view]) => applyHostCost(held, window, view), emptyState);
          for (const window of windows) {
            const newest = sent
              .filter(([w]) => w === window)
              .reduce<HostCost | undefined>(
                (best, [, cost]) =>
                  best === undefined || cost.sequence >= best.sequence ? cost : best,
                undefined,
              );
            expect(state.hostCosts[window]).toEqual(
              newest === undefined ? undefined : viewOf(arrives(newest)),
            );
          }
          expect({...state, hostCosts: {}}).toEqual(emptyState);
        },
      ),
      {seed: 11},
    );
  });

  it('are not replaced by an older view, whatever order the views arrive in', () => {
    fc.assert(
      fc.property(hostCost, fc.bigInt({min: 1n, max: 1000n}), (cost, ahead) => {
        const older = viewOf(arrives(cost));
        const newer = viewOf(
          arrives(create(HostCostSchema, {...cost, sequence: cost.sequence + ahead})),
        );
        const held = applyHostFrame(emptyHostModel, {
          from: 'host-cost',
          phase: 'update',
          window: 'all',
          view: newer,
        });
        const after = applyHostFrame(held, {
          from: 'host-cost',
          phase: 'update',
          window: 'all',
          view: older,
        });
        expect(after.data.hostCosts.all).toBe(newer);
      }),
      {seed: 13},
    );
  });

  it('let a new stream replace a held view from a later sequence', () => {
    fc.assert(
      fc.property(hostCost, fc.bigInt({min: 1n, max: 1000n}), (cost, ahead) => {
        const initial = viewOf(arrives(cost));
        const beforeRestart = viewOf(
          arrives(create(HostCostSchema, {...cost, sequence: cost.sequence + ahead})),
        );
        const held = applyHostFrame(emptyHostModel, {
          from: 'host-cost',
          phase: 'update',
          window: 'all',
          view: beforeRestart,
        });
        const restarted = applyHostFrame(held, {
          from: 'host-cost',
          phase: 'initial',
          window: 'all',
          view: initial,
        });
        expect(restarted.data.hostCosts.all).toEqual(initial);
      }),
      {seed: 17},
    );
  });
});
