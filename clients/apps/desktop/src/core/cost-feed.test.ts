import {create, fromBinary} from '@bufbuild/protobuf';
import {timestampFromMs} from '@bufbuild/protobuf/wkt';
import {describe, expect, it} from '@effect/vitest';
import {
  AgentRole,
  Harness,
  RoleCostSchema,
  RunCostSchema,
  SpendBucketSchema,
  UsageSchema,
  WatchRunCostRequestSchema,
} from '@vibesys/orchestrator-proto';
import * as Effect from 'effect/Effect';
import {costViewOf, type FeedStatus} from '../state/index.js';
import {
  FakeDaemon,
  generateScenario,
  type HostHarness,
  hostHarness,
  methodPathOf,
} from '../testing/orchestrator/index.js';

const scenario = generateScenario({seed: 7, subagents: 2, live: false});
const RUN = scenario.runId;

/** The view the host would send: the Fake works out nothing, so the numbers are the test's. */
const hostCost = (costUsd: number, sequence = 3n) =>
  create(RunCostSchema, {
    runId: RUN,
    sequence,
    asOf: timestampFromMs(Date.parse('2026-11-01T19:00:00Z')),
    total: create(UsageSchema, {costUsd, outputTokens: 900n}),
    turns: 4,
    unpricedTurns: 1,
    maxCostUsd: 500,
    roles: [
      create(RoleCostSchema, {
        role: AgentRole.LEAD,
        agents: 1,
        turns: 4,
        unpricedTurns: 1,
        usage: create(UsageSchema, {costUsd, outputTokens: 900n}),
      }),
    ],
    series: {
      start: timestampFromMs(Date.parse('2026-11-01T18:00:00Z')),
      width: {seconds: 3600n, nanos: 0},
      buckets: [
        create(SpendBucketSchema, {
          start: timestampFromMs(Date.parse('2026-11-01T18:00:00Z')),
          costUsd,
          byModel: [{model: {harness: Harness.CLAUDE_CODE, model: ''}, costUsd}],
        }),
      ],
    },
    lastHourUsd: costUsd,
  });

const costCalls = (h: HostHarness) =>
  h.fake.calls().filter(call => call.target.method === methodPathOf('WatchRunCost'));
const openStreams = (h: HostHarness) =>
  h.fake.calls().filter(call => call.kind === 'stream' && call.open);

describe('a run cost stream', () => {
  it.effect('asks for the run it is mounted for, and holds the view the host sent as it is', () =>
    Effect.gen(function* () {
      const daemon = new FakeDaemon(scenario);
      const sent = hostCost(12.5);
      daemon.publishCost(sent);
      const h = yield* hostHarness(daemon);
      h.mount(h.session.costs.watch(RUN));
      yield* h.elapse(1);
      const [call] = costCalls(h);
      expect(call && fromBinary(WatchRunCostRequestSchema, call.target.request).runId).toBe(RUN);
      expect(yield* h.read(h.session.costs.status(RUN))).toEqual({_tag: 'Live'});
      expect(yield* h.read(h.session.costs.freshness(RUN))).toBe('live');
      const held = (yield* h.model).data.costs[RUN];
      expect(held).toEqual(costViewOf(sent));
      // The numbers a screen shows are the host's, never a sum of its parts.
      expect(held?.total.costUsd).toBe(12.5);
      expect(held?.maxCostUsd).toBe(500);
      expect(held?.unpricedTurns).toBe(1);
      expect(held?.lastHourUsd).toBe(12.5);
    }),
  );

  it.effect('closes its stream when unmounted and reads a newer view when mounted again', () =>
    Effect.gen(function* () {
      const daemon = new FakeDaemon(scenario);
      daemon.publishCost(hostCost(1));
      const h = yield* hostHarness(daemon);
      const unmount = h.mount(h.session.costs.watch(RUN));
      yield* h.elapse(1);
      expect(openStreams(h)).toHaveLength(1);
      unmount();
      yield* h.elapse(1);
      expect(openStreams(h)).toHaveLength(0);
      expect(yield* h.read(h.session.costs.status(RUN))).toEqual({_tag: 'Opening'});
      daemon.publishCost(hostCost(2, 9n));
      h.mount(h.session.costs.watch(RUN));
      yield* h.elapse(1);
      expect((yield* h.model).data.costs[RUN]?.total.costUsd).toBe(2);
    }),
  );

  it.effect('fails alone for a run the host does not have, and reopens on the user retry', () =>
    Effect.gen(function* () {
      const daemon = new FakeDaemon(scenario);
      const h = yield* hostHarness(daemon);
      h.mount(h.session.runs.watch);
      h.mount(h.session.costs.watch(RUN));
      yield* h.elapse(30);
      const failed: FeedStatus = yield* h.read(h.session.costs.status(RUN));
      expect(failed).toMatchObject({_tag: 'Failed', fault: {kind: 'NotFound'}});
      expect(costCalls(h)).toHaveLength(1);
      expect(yield* h.read(h.session.runs.status)).toEqual({_tag: 'Live'});
      daemon.publishCost(hostCost(3));
      yield* h.session.costs.retry(RUN);
      yield* h.elapse(1);
      expect(costCalls(h)).toHaveLength(2);
      expect((yield* h.model).data.costs[RUN]?.total.costUsd).toBe(3);
    }),
  );
});
