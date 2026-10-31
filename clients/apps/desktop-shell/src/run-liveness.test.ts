import {expect, test} from 'bun:test';
import {create} from '@bufbuild/protobuf';
import type {HostKey} from '@vibesys/hosts';
import {RunHeadSchema, RunLiveness, TopicEntrySchema} from '@vibesys/proto';
import {
  CONNECT_STEPS,
  type HostRunFacts,
  RETRYABLE_REASONS,
  type RunFacts,
  runs,
  type SubscriptionState,
  TERMINAL_REASONS,
} from '@vibesys/subscription';
import {FakeSubscriptionPort} from '@vibesys/subscription/testing';
import fc from 'fast-check';
import {livenessOf, runLiveness} from './run-liveness.js';

const HOST = 'ssh:host-a' as HostKey;
const RUN = 'run-a';

const FACTS: fc.Arbitrary<RunFacts> = fc.oneof(
  fc.record({
    projectRoot: fc.constant(undefined),
    head: fc.integer({min: 0, max: 9}).map(epoch => create(RunHeadSchema, {epoch})),
    liveness: fc.constantFrom(
      RunLiveness.ALIVE,
      RunLiveness.INTERRUPTED,
      RunLiveness.ENDED,
      RunLiveness.PARKED,
    ),
  }),
  fc.record({
    projectRoot: fc.constant(undefined),
    head: fc.constant(undefined),
    liveness: fc.constant(RunLiveness.UNSPECIFIED),
  }),
);
const FACT_MAP: fc.Arbitrary<HostRunFacts> = fc
  .array(fc.tuple(fc.constantFrom(RUN, 'run-b', 'run-c'), FACTS))
  .map(entries => new Map(entries));

const STATE: fc.Arbitrary<SubscriptionState<HostRunFacts>> = fc.oneof(
  fc.record({
    kind: fc.constant('connecting' as const),
    step: fc.constantFrom(...CONNECT_STEPS),
    attempt: fc.nat({max: 6}),
  }),
  fc.record({kind: fc.constant('live' as const), value: FACT_MAP, since: fc.nat()}),
  fc.record({
    kind: fc.constant('stale' as const),
    value: FACT_MAP,
    reason: fc.constantFrom(...RETRYABLE_REASONS),
    attempt: fc.integer({min: 1, max: 6}),
    valueAt: fc.nat(),
  }),
  fc.record({
    kind: fc.constant('failed' as const),
    reason: fc.constantFrom(...RETRYABLE_REASONS, ...TERMINAL_REASONS),
    retrying: fc.boolean(),
  }),
);

test('property: only a live subscription is a word about a run, and it is that run’s own', () => {
  fc.assert(
    fc.property(STATE, state => {
      const heard = livenessOf(RUN, state);
      expect(heard.kind === 'live').toBe(state.kind === 'live');
      if (state.kind === 'live' && heard.kind === 'live') {
        const facts = state.value.get(RUN);
        expect(heard.hint === undefined).toBe(facts === undefined);
        expect(heard.hint?.value).toBe(facts?.liveness);
        if (facts !== undefined) expect(heard.hint?.epoch).toBe(facts.head?.epoch ?? 0);
      }
      // A subscription that has lost a stream, or given up, is no word about anything.
      if (state.kind === 'stale' || state.kind === 'failed') expect(heard.kind).toBe('lost');
    }),
    {numRuns: 100},
  );
});

test('the feed opens the run’s key on the host’s runs, gives one object per state, and closes with its last listener', () => {
  const port = new FakeSubscriptionPort();
  const feed = runLiveness(port, HOST)(RUN);
  expect(port.calls).toHaveLength(0);
  expect(feed.getSnapshot()).toEqual({kind: 'waiting'});
  let heard = 0;
  const release = feed.subscribe(() => {
    heard += 1;
  });
  const [call] = port.calls;
  expect(call).toMatchObject({host: HOST, key: RUN, open: true});
  expect(call?.request).toEqual(runs.request);
  call?.send({kind: 'begin'});
  call?.send({
    kind: 'entry',
    entry: create(TopicEntrySchema, {
      key: RUN,
      value: {case: 'runs', value: {liveness: RunLiveness.ALIVE, head: {epoch: 3}}},
    }),
  });
  call?.send({kind: 'synced'});
  call?.send({kind: 'control', control: {kind: 'live', since: 1}});
  const live = feed.getSnapshot();
  expect(live).toMatchObject({kind: 'live', hint: {value: RunLiveness.ALIVE, epoch: 3}});
  expect(feed.getSnapshot()).toBe(live);
  expect(heard).toBeGreaterThan(0);
  call?.send({kind: 'control', control: {kind: 'stale', reason: 'silent', attempt: 1, valueAt: 1}});
  expect(feed.getSnapshot()).toEqual({kind: 'lost'});
  release();
  expect(call?.open).toBe(false);
});

test('a port that main ended is a lost host, not a hang', () => {
  const port = new FakeSubscriptionPort();
  const feed = runLiveness(port, HOST)(RUN);
  const release = feed.subscribe(() => {});
  port.calls[0]?.end('gone');
  expect(feed.getSnapshot()).toEqual({kind: 'lost'});
  release();
});
