import {create, fromBinary, toBinary} from '@bufbuild/protobuf';
import {timestampFromMs, timestampMs} from '@bufbuild/protobuf/wkt';
import {describe, expect, it} from '@effect/vitest';
import {
  AgentRole,
  Harness,
  HostCostSchema,
  HostRunCostSchema,
  RoleCostSchema,
  RunStatus,
  SpendBucketSchema,
  UsageSchema,
  WatchHostCostRequestSchema,
  WatchHostCostResponseSchema,
} from '@vibesys/orchestrator-proto';
import * as Effect from 'effect/Effect';
import {type FeedStatus, readHostCost, sinceWindow} from '../state/index.js';
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

const WHEN = Date.parse('2026-11-01T19:00:00Z');

/** The view the host would send: the Fake works out nothing, so the numbers are the test's. */
const hostCost = (costUsd: number, sequence = 3n) =>
  create(HostCostSchema, {
    sequence,
    asOf: timestampFromMs(WHEN),
    total: create(UsageSchema, {costUsd, outputTokens: 900n}),
    runCount: 2,
    turns: 4,
    unpricedTurns: 1,
    runs: [
      create(HostRunCostSchema, {
        runId: 'run-a',
        name: 'the first task',
        status: RunStatus.RUNNING,
        maxCostUsd: 500,
        turns: 3,
        usage: create(UsageSchema, {costUsd, outputTokens: 900n}),
        totalTokens: 900n,
      }),
      create(HostRunCostSchema, {
        runId: 'run-b',
        name: 'the second task',
        status: RunStatus.ENDED,
        turns: 1,
        unpricedTurns: 1,
        usage: create(UsageSchema, {}),
      }),
    ],
    roles: [
      create(RoleCostSchema, {
        role: AgentRole.LEAD,
        agents: 2,
        turns: 4,
        unpricedTurns: 1,
        usage: create(UsageSchema, {costUsd, outputTokens: 900n}),
      }),
    ],
    series: {
      start: timestampFromMs(WHEN - 3_600_000),
      width: {seconds: 3600n, nanos: 0},
      buckets: [
        create(SpendBucketSchema, {
          start: timestampFromMs(WHEN - 3_600_000),
          costUsd,
          byModel: [{model: {harness: Harness.CLAUDE_CODE, model: ''}, costUsd}],
        }),
      ],
    },
    lastHourUsd: costUsd,
  });

const hostCostCalls = (h: HostHarness) =>
  h.fake.calls().filter(call => call.target.method === methodPathOf('WatchHostCost'));
const openStreams = (h: HostHarness) =>
  h.fake.calls().filter(call => call.kind === 'stream' && call.open);

describe('the host cost stream', () => {
  it.effect(
    'asks for the window it is mounted for, and holds the view the host sent as it is',
    () =>
      Effect.gen(function* () {
        const daemon = new FakeDaemon(scenario);
        const sent = hostCost(12.5);
        daemon.publishHostCost(sent);
        const h = yield* hostHarness(daemon);
        h.mount(h.session.hostCosts.watch('all'));
        yield* h.elapse(1);
        const [call] = hostCostCalls(h);
        // Everything the host holds is a request with no window.
        expect(
          call && fromBinary(WatchHostCostRequestSchema, call.target.request).since,
        ).toBeUndefined();
        expect(yield* h.read(h.session.hostCosts.status('all'))).toEqual({_tag: 'Live'});
        expect(yield* h.read(h.session.hostCosts.freshness('all'))).toBe('live');
        const held = (yield* h.model).data.hostCosts.all;
        const read = readHostCost(sent);
        expect(read._tag).toBe('View');
        expect(held).toEqual(read._tag === 'View' ? read.view : undefined);
        // The numbers a screen shows are the host's, never a sum of its parts.
        expect(held?.total.costUsd).toBe(12.5);
        expect(held?.runCount).toBe(2);
        expect(held?.unpricedTurns).toBe(1);
        expect(held?.runs.map(run => run.runId)).toEqual(['run-a', 'run-b']);
        expect(held?.runs[1]?.usage.costUsd).toBeNull();
        expect(held?.lastHourUsd).toBe(12.5);
      }),
  );

  it.effect('asks for a window by the instant it begins, and holds each window apart', () =>
    Effect.gen(function* () {
      const daemon = new FakeDaemon(scenario);
      const since = WHEN - 7 * 24 * 3_600_000;
      daemon.publishHostCost(hostCost(10));
      daemon.publishHostCost(hostCost(2), since);
      const h = yield* hostHarness(daemon);
      const week = sinceWindow(since);
      h.mount(h.session.hostCosts.watch('all'));
      h.mount(h.session.hostCosts.watch(week));
      yield* h.elapse(1);
      const asked = hostCostCalls(h).map(call => {
        const {since: at} = fromBinary(WatchHostCostRequestSchema, call.target.request);
        return at === undefined ? null : timestampMs(at);
      });
      expect(asked.sort()).toEqual([null, since].sort());
      const {hostCosts} = (yield* h.model).data;
      expect(hostCosts.all?.total.costUsd).toBe(10);
      expect(hostCosts[week]?.total.costUsd).toBe(2);
    }),
  );

  it.effect('closes its stream when unmounted and reads a newer view when mounted again', () =>
    Effect.gen(function* () {
      const daemon = new FakeDaemon(scenario);
      daemon.publishHostCost(hostCost(1));
      const h = yield* hostHarness(daemon);
      const unmount = h.mount(h.session.hostCosts.watch('all'));
      yield* h.elapse(1);
      expect(openStreams(h)).toHaveLength(1);
      unmount();
      yield* h.elapse(1);
      expect(openStreams(h)).toHaveLength(0);
      expect(yield* h.read(h.session.hostCosts.status('all'))).toEqual({_tag: 'Opening'});
      daemon.publishHostCost(hostCost(2, 9n));
      h.mount(h.session.hostCosts.watch('all'));
      yield* h.elapse(1);
      expect((yield* h.model).data.hostCosts.all?.total.costUsd).toBe(2);
    }),
  );

  it.effect('fails alone for a window the host refuses, and reopens on the user retry', () =>
    Effect.gen(function* () {
      const daemon = new FakeDaemon(scenario);
      const h = yield* hostHarness(daemon);
      h.mount(h.session.runs.watch);
      h.mount(h.session.hostCosts.watch('all'));
      yield* h.elapse(30);
      const failed: FeedStatus = yield* h.read(h.session.hostCosts.status('all'));
      expect(failed).toMatchObject({_tag: 'Failed', fault: {kind: 'NotFound'}});
      expect(hostCostCalls(h)).toHaveLength(1);
      expect(yield* h.read(h.session.runs.status)).toEqual({_tag: 'Live'});
      daemon.publishHostCost(hostCost(3));
      yield* h.session.hostCosts.retry('all');
      yield* h.elapse(1);
      expect(hostCostCalls(h)).toHaveLength(2);
      expect((yield* h.model).data.hostCosts.all?.total.costUsd).toBe(3);
    }),
  );

  it.effect(
    'fails with the field named when a view lacks one the contract requires, and holds none',
    () =>
      Effect.gen(function* () {
        const daemon = new FakeDaemon(scenario);
        // A host that broke the contract: the view has no series.
        daemon.publishHostCost(create(HostCostSchema, {...hostCost(5), series: undefined}));
        const h = yield* hostHarness(daemon);
        h.mount(h.session.runs.watch);
        h.mount(h.session.hostCosts.watch('all'));
        yield* h.elapse(30);
        const status = yield* h.read(h.session.hostCosts.status('all'));
        expect(status).toMatchObject({
          _tag: 'Failed',
          fault: {kind: 'InvalidMessage', retryable: false},
        });
        expect(status._tag === 'Failed' ? status.fault.detail : '').toContain('series');
        // No view is made of it: nothing is held, and the rest of the app carries on.
        expect((yield* h.model).data.hostCosts.all).toBeUndefined();
        expect(yield* h.read(h.session.hostCosts.freshness('all'))).toBe('unavailable');
        expect(yield* h.read(h.session.runs.status)).toEqual({_tag: 'Live'});
      }),
  );

  it.effect(
    'a host without the method fails the host cost stream alone and leaves the link up',
    () =>
      Effect.gen(function* () {
        const daemon = new FakeDaemon(scenario);
        const h = yield* hostHarness(daemon, {
          wrap: base => ({
            respond: (target, nth) =>
              target.method === methodPathOf('WatchHostCost')
                ? [{end: {message: 'no such method', status: RpcStatus.UNIMPLEMENTED}}]
                : base.respond(target, nth),
          }),
        });
        h.mount(h.session.runs.watch);
        h.mount(h.session.hostCosts.watch('all'));
        yield* h.elapse(30);
        expect(yield* h.read(h.session.hostCosts.status('all'))).toMatchObject({
          _tag: 'Failed',
          fault: {kind: 'Unimplemented', retryable: false},
        });
        expect(yield* h.read(h.session.hostCosts.freshness('all'))).toBe('unavailable');
        expect((yield* h.model).data.hostCosts.all).toBeUndefined();
        // The rest of the app is unaffected: the run stream is live, and the cost stream is not retried.
        expect(yield* h.read(h.session.runs.status)).toEqual({_tag: 'Live'});
        expect(yield* h.read(h.session.runs.freshness)).toBe('live');
        expect(hostCostCalls(h)).toHaveLength(1);
      }),
  );

  it.effect('opens the stream again after the link drops and then holds the host view', () =>
    Effect.gen(function* () {
      const daemon = new FakeDaemon(scenario);
      daemon.publishHostCost(hostCost(7));
      const h = yield* hostHarness(daemon, {
        // The first stream loses the link before its view arrives; the second is whole.
        wrap: faultsOn('WatchHostCost', {dropAfter: [0], duplicate: []}),
      });
      h.mount(h.session.hostCosts.watch('all'));
      yield* h.elapse(60);
      expect(hostCostCalls(h).length).toBeGreaterThan(1);
      expect((yield* h.model).data.hostCosts.all?.total.costUsd).toBe(7);
      expect(yield* h.read(h.session.hostCosts.freshness('all'))).toBe('live');
    }),
  );

  it.effect('takes the first whole view after restart, then rejects an older update', () =>
    Effect.gen(function* () {
      const daemon = new FakeDaemon(scenario);
      daemon.publishHostCost(hostCost(1, 9n));
      const h = yield* hostHarness(daemon, {
        wrap: base => ({
          respond: (target, nth) => {
            const steps = base.respond(target, nth);
            if (target.method !== methodPathOf('WatchHostCost')) return steps;
            if (nth === 0) {
              daemon.publishHostCost(hostCost(2, 1n));
              return [
                ...steps.filter(step => 'frame' in step),
                {end: {message: 'the host restarted', status: RpcStatus.UNAVAILABLE}},
              ];
            }
            if (nth === 1) {
              const stale = create(WatchHostCostResponseSchema, {cost: hostCost(3, 0n)});
              return [
                ...steps.filter(step => 'frame' in step),
                {frame: toBinary(WatchHostCostResponseSchema, stale)},
                {hang: true},
              ];
            }
            return steps;
          },
        }),
      });
      h.mount(h.session.hostCosts.watch('all'));
      yield* h.elapse(60);
      expect(hostCostCalls(h).length).toBeGreaterThan(1);
      expect((yield* h.model).data.hostCosts.all?.total.costUsd).toBe(2);
      expect(yield* h.read(h.session.hostCosts.freshness('all'))).toBe('live');
    }),
  );
});
