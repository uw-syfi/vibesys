import {expect, test} from 'bun:test';
import {
  CONNECTING,
  machineStats,
  type SubscriptionState,
  type SubscriptionUpdate,
  subscriptionSource,
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
  call()?.send({kind: 'control', control: {kind: 'stale', reason: 'silent', attempt: 1}});
  const stale = source.getSnapshot();
  expect(stale).toEqual({kind: 'stale', value, reason: 'silent', attempt: 1});
  call()?.send({kind: 'control', control: {kind: 'failed', reason: 'unreachable', retrying: true}});
  expect(source.getSnapshot()).toEqual({kind: 'failed', reason: 'unreachable', retrying: true});
  expect(states.map(each => each.kind)).toEqual(['live', 'stale', 'failed']);
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
