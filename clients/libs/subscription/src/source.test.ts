import {expect, test} from 'bun:test';
import fc from 'fast-check';
import {
  CONNECTING,
  type Control,
  machineStats,
  RETRYABLE_REASONS,
  type SubscriptionState,
  type SubscriptionUpdate,
  subscriptionSource,
  TERMINAL_REASONS,
} from './index.js';
import {statsEntry} from './testing/harness.js';
import {FakeSubscriptionPort} from './testing/index.js';

const HOST = 'ssh:host-a';

const DATA: readonly SubscriptionUpdate[] = [
  {kind: 'begin'},
  {kind: 'entry', entry: statsEntry('machine', 5)},
  {kind: 'synced'},
];

function watched() {
  const port = new FakeSubscriptionPort();
  const source = subscriptionSource(port, HOST, machineStats);
  const states: SubscriptionState<unknown>[] = [];
  const unsubscribe = source.subscribe(() => states.push(source.getSnapshot()));
  return {port, source, states, unsubscribe, call: () => port.calls[0]};
}

test('the first listener opens a port for its topic and the last one closes it', () => {
  const port = new FakeSubscriptionPort();
  const source = subscriptionSource(port, HOST, machineStats, 'machine');
  expect(source.getSnapshot()).toBe(CONNECTING);
  expect(port.calls).toHaveLength(0);
  const first = source.subscribe(() => {});
  const second = source.subscribe(() => {});
  expect(port.calls).toHaveLength(1);
  expect(port.calls[0]).toMatchObject({host: HOST, key: 'machine'});
  expect(port.calls[0]?.request).toBe(machineStats.request);
  first();
  expect(port.held).toHaveLength(1);
  second();
  expect(port.held).toHaveLength(0);
  // Mounting again starts over rather than showing what the last mount knew.
  expect(source.getSnapshot()).toBe(CONNECTING);
  source.subscribe(() => {});
  expect(port.calls).toHaveLength(2);
});

test("it is live only once the snapshot and main's word agree, and stale keeps the value", () => {
  const {source, states, call} = watched();
  const live = {kind: 'live', since: 7} as const;
  for (const update of DATA) call()?.send(update);
  // The snapshot alone shows nothing: main has not said it is live.
  expect(source.getSnapshot().kind).toBe('connecting');
  call()?.send({kind: 'control', control: live});
  const state = source.getSnapshot();
  if (state.kind !== 'live') throw new Error(`expected live, got ${state.kind}`);
  const {value} = state;
  expect(value.sampledAt?.nanos).toBe(5_000_000);
  call()?.send({
    kind: 'control',
    control: {kind: 'stale', reason: 'silent', attempt: 1, valueAt: 9},
  });
  const stale = source.getSnapshot();
  expect(stale).toEqual({kind: 'stale', value, reason: 'silent', attempt: 1, valueAt: 9});
  call()?.send({kind: 'control', control: {kind: 'failed', reason: 'unreachable', retrying: true}});
  // The failure keeps what the host last said, to be shown as old; it is not a value gone.
  expect(source.getSnapshot()).toEqual({
    kind: 'failed',
    reason: 'unreachable',
    retrying: true,
    held: value,
  });
  expect(states.map(each => each.kind)).toEqual(['live', 'stale', 'failed']);
});

test('a host that never spoke fails holding nothing', () => {
  const {source, call} = watched();
  call()?.send({kind: 'control', control: {kind: 'failed', reason: 'unreachable', retrying: true}});
  expect(source.getSnapshot()).toEqual({kind: 'failed', reason: 'unreachable', retrying: true});
});

/** What a state holds of the host's value: current, stale or held through a failure. */
function heldBy<S>(state: SubscriptionState<S>): S | undefined {
  if (state.kind === 'failed') return state.held;
  return state.kind === 'connecting' ? undefined : state.value;
}

test('property: through any run of losses and recoveries the value a host last said is the one held', () => {
  const CONTROLS: fc.Arbitrary<Control> = fc.oneof(
    fc.nat({max: 20}).map(since => ({kind: 'live', since}) as const),
    fc
      .tuple(fc.constantFrom(...RETRYABLE_REASONS), fc.nat({max: 40}), fc.nat({max: 20}))
      .map(([reason, attempt, valueAt]) => ({kind: 'stale', reason, attempt, valueAt}) as const),
    fc
      .tuple(fc.constantFrom(...RETRYABLE_REASONS, ...TERMINAL_REASONS), fc.boolean())
      .map(([reason, retrying]) => ({kind: 'failed', reason, retrying}) as const),
  );
  fc.assert(
    fc.property(fc.array(CONTROLS, {minLength: 1, maxLength: 60}), controls => {
      const {source, call} = watched();
      for (const update of DATA) call()?.send(update);
      call()?.send({kind: 'control', control: {kind: 'live', since: 0}});
      const said = source.getSnapshot();
      if (said.kind !== 'live') throw new Error('expected live');
      for (const control of controls) {
        call()?.send({kind: 'control', control});
        // The value is the one said, in whatever state the losses left the subscription; a failure
        // with no attempt to come holds nothing.
        expect(heldBy(source.getSnapshot())).toEqual(
          control.kind === 'failed' && !control.retrying ? undefined : said.value,
        );
      }
    }),
  );
});

test('a snapshot still being built, and a repeated control, change nothing a screen can see', () => {
  const {source, states, call} = watched();
  for (const update of DATA) call()?.send(update);
  call()?.send({kind: 'control', control: {kind: 'live', since: 1}});
  const before = source.getSnapshot();
  call()?.send({kind: 'control', control: {kind: 'live', since: 1}});
  call()?.send({kind: 'begin'});
  call()?.send({kind: 'entry', entry: statsEntry('machine', 6)});
  expect(source.getSnapshot()).toBe(before);
  expect(states).toHaveLength(1);
  call()?.send({kind: 'synced'});
  expect(source.getSnapshot()).not.toBe(before);
  expect(states).toHaveLength(2);
});

test('a closed port is failed closed, and a broken frame is failed invalid, never a value', () => {
  const closed = watched();
  for (const update of DATA) closed.call()?.send(update);
  closed.call()?.send({kind: 'control', control: {kind: 'live', since: 1}});
  closed.call()?.end('port closed');
  expect(closed.source.getSnapshot()).toEqual({kind: 'failed', reason: 'closed', retrying: false});
  closed.call()?.send({kind: 'control', control: {kind: 'live', since: 2}});
  expect(closed.source.getSnapshot().kind).toBe('failed');

  const broken = watched();
  broken.call()?.send({kind: 'entry', entry: statsEntry('machine', 1)});
  broken.call()?.send({kind: 'control', control: {kind: 'live', since: 1}});
  expect(broken.source.getSnapshot()).toEqual({
    kind: 'failed',
    reason: 'invalid_frame',
    retrying: false,
  });
});

/** Every screen must branch on all four kinds; a new kind is a compile error here. */
function wording(state: SubscriptionState<unknown>): string {
  switch (state.kind) {
    case 'connecting':
      return `connecting ${state.step}`;
    case 'live':
      return 'live';
    case 'stale':
      return `stale ${state.reason}`;
    case 'failed':
      return `failed ${state.reason}`;
    default: {
      const unreachable: never = state;
      return unreachable;
    }
  }
}

test('the state type names each way a subscription can be', () => {
  expect(wording(CONNECTING)).toBe('connecting gateway');
});

test('the state is computed when it is read, once for a burst of updates, and again only after a change', () => {
  const port = new FakeSubscriptionPort();
  let folds = 0;
  const counting = {
    ...machineStats,
    fold: (entries: Parameters<typeof machineStats.fold>[0]) => {
      folds += 1;
      return machineStats.fold(entries);
    },
  };
  const source = subscriptionSource(port, HOST, counting);
  const woken: number[] = [];
  source.subscribe(() => woken.push(woken.length));
  const call = port.calls[0];
  for (const update of DATA) call?.send(update);
  call?.send({kind: 'control', control: {kind: 'live', since: 1}});
  const afterSnapshot = folds;
  for (let at = 10; at < 60; at += 1) call?.send({kind: 'entry', entry: statsEntry('machine', at)});
  // A burst of fifty changes costs no fold until a screen reads...
  expect(folds).toBe(afterSnapshot);
  // ...and one when it does, however many reads follow.
  const state = source.getSnapshot();
  expect(source.getSnapshot()).toBe(state);
  expect(folds).toBe(afterSnapshot + 1);
  expect(state.kind === 'live' ? state.value.sampledAt?.nanos : null).toBe(59_000_000);
  call?.send({kind: 'entry', entry: statsEntry('machine', 99)});
  expect(source.getSnapshot()).not.toBe(state);
  expect(folds).toBe(afterSnapshot + 2);
});
