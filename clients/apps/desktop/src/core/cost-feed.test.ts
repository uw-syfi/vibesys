import {create, fromBinary, toBinary} from '@bufbuild/protobuf';
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
  WatchRunCostResponseSchema,
} from '@vibesys/orchestrator-proto';
import * as Effect from 'effect/Effect';
import {type FeedStatus, readCost} from '../state/index.js';
import {
  FakeDaemon,
  faultsOn,
  generateScenario,
  type HostHarness,
  hostHarness,
  methodPathOf,
} from '../testing/orchestrator/index.js';
import {RpcStatus} from './errors.js';

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
      const read = readCost(sent);
      expect(read._tag).toBe('View');
      expect(held).toEqual(read._tag === 'View' ? read.view : undefined);
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

  it.effect(
    'fails with the field named when a view lacks one the contract requires, and holds none',
    () =>
      Effect.gen(function* () {
        const daemon = new FakeDaemon(scenario);
        // A host that broke the contract: the view has no series.
        daemon.publishCost(create(RunCostSchema, {...hostCost(5), series: undefined}));
        const h = yield* hostHarness(daemon);
        h.mount(h.session.runs.watch);
        h.mount(h.session.costs.watch(RUN));
        yield* h.elapse(30);
        const status = yield* h.read(h.session.costs.status(RUN));
        expect(status).toMatchObject({
          _tag: 'Failed',
          fault: {kind: 'InvalidMessage', retryable: false},
        });
        expect(status._tag === 'Failed' ? status.fault.detail : '').toContain('series');
        // No view is made of it: nothing is held, and the rest of the app carries on.
        expect((yield* h.model).data.costs[RUN]).toBeUndefined();
        expect(yield* h.read(h.session.costs.freshness(RUN))).toBe('unavailable');
        expect(yield* h.read(h.session.runs.status)).toEqual({_tag: 'Live'});
      }),
  );

  it.effect('a host without the method fails the cost stream alone and leaves the link up', () =>
    Effect.gen(function* () {
      const daemon = new FakeDaemon(scenario);
      const h = yield* hostHarness(daemon, {
        wrap: base => ({
          respond: (target, nth) =>
            target.method === methodPathOf('WatchRunCost')
              ? [{end: {message: 'no such method', status: RpcStatus.UNIMPLEMENTED}}]
              : base.respond(target, nth),
        }),
      });
      h.mount(h.session.runs.watch);
      h.mount(h.session.costs.watch(RUN));
      yield* h.elapse(30);
      expect(yield* h.read(h.session.costs.status(RUN))).toMatchObject({
        _tag: 'Failed',
        fault: {kind: 'Unimplemented', retryable: false},
      });
      expect(yield* h.read(h.session.costs.freshness(RUN))).toBe('unavailable');
      expect((yield* h.model).data.costs[RUN]).toBeUndefined();
      // The rest of the app is unaffected: the run stream is live, and the cost stream is not retried.
      expect(yield* h.read(h.session.runs.status)).toEqual({_tag: 'Live'});
      expect(yield* h.read(h.session.runs.freshness)).toBe('live');
      expect(costCalls(h)).toHaveLength(1);
    }),
  );

  it.effect('opens the stream again after the link drops and then holds the host view', () =>
    Effect.gen(function* () {
      const daemon = new FakeDaemon(scenario);
      daemon.publishCost(hostCost(7));
      const h = yield* hostHarness(daemon, {
        // The first stream loses the link before its view arrives; the second is whole.
        wrap: faultsOn('WatchRunCost', {dropAfter: [0], duplicate: []}),
      });
      h.mount(h.session.costs.watch(RUN));
      yield* h.elapse(60);
      expect(costCalls(h).length).toBeGreaterThan(1);
      expect((yield* h.model).data.costs[RUN]?.total.costUsd).toBe(7);
      expect(yield* h.read(h.session.costs.freshness(RUN))).toBe('live');
    }),
  );

  it.effect('takes the first whole view after restart, then rejects an older update', () =>
    Effect.gen(function* () {
      const daemon = new FakeDaemon(scenario);
      daemon.publishCost(hostCost(1, 9n));
      const h = yield* hostHarness(daemon, {
        wrap: base => ({
          respond: (target, nth) => {
            const steps = base.respond(target, nth);
            if (target.method !== methodPathOf('WatchRunCost')) return steps;
            if (nth === 0) {
              daemon.publishCost(hostCost(2, 1n));
              return [
                ...steps.filter(step => 'frame' in step),
                {end: {message: 'the host restarted', status: RpcStatus.UNAVAILABLE}},
              ];
            }
            if (nth === 1) {
              const stale = create(WatchRunCostResponseSchema, {cost: hostCost(3, 0n)});
              return [
                ...steps.filter(step => 'frame' in step),
                {frame: toBinary(WatchRunCostResponseSchema, stale)},
                {hang: true},
              ];
            }
            return steps;
          },
        }),
      });
      h.mount(h.session.costs.watch(RUN));
      yield* h.elapse(60);
      expect(costCalls(h).length).toBeGreaterThan(1);
      expect((yield* h.model).data.costs[RUN]?.total.costUsd).toBe(2);
      expect(yield* h.read(h.session.costs.freshness(RUN))).toBe('live');
    }),
  );
});
