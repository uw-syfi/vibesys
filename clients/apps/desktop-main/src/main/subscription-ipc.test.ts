import {expect, test} from 'bun:test';
import {decodeGatewayReply, decodeUpdate, encodeSubscriptionCall} from '@vibesys/ipc';
import {machineStats, type SubscriptionUpdate} from '@vibesys/subscription';
import {FakeSubscriptionPort} from '@vibesys/subscription/testing';
import {subscriptionCall} from './subscription-ipc.js';

const REQUEST = encodeSubscriptionCall({
  kind: 'observe',
  host: 'ssh:host-a',
  request: machineStats.request,
  key: undefined,
});

function updatesIn(sent: readonly Uint8Array[]): SubscriptionUpdate[] {
  return sent.map(reply => {
    const body = decodeGatewayReply(reply);
    if (body.case !== 'bytes') throw new Error('not an update');
    return decodeUpdate(body.value);
  });
}

test('a window observes the topic it asked for and is sent each update as protobuf', async () => {
  const hub = new FakeSubscriptionPort();
  const sent: Uint8Array[] = [];
  const done = subscriptionCall(hub)(REQUEST, new AbortController().signal, reply =>
    sent.push(reply),
  );
  const [call] = hub.calls;
  expect(call).toMatchObject({host: 'ssh:host-a', key: undefined});
  expect(call?.request.topic.case).toBe('machineStats');
  call?.send({kind: 'begin'});
  call?.send({kind: 'control', control: {kind: 'live', since: 3}});
  expect(updatesIn(sent).map(update => update.kind)).toEqual(['begin', 'control']);
  call?.end();
  await done;
});

test('the hub ending the observation with an error fails the call, and without one ends it', async () => {
  const hub = new FakeSubscriptionPort();
  const failing = subscriptionCall(hub)(REQUEST, new AbortController().signal, () => {});
  hub.calls[0]?.end('host removed');
  await expect(failing).rejects.toThrow('host removed');
  const quiet = subscriptionCall(hub)(REQUEST, new AbortController().signal, () => {});
  hub.calls[1]?.end();
  await quiet;
});

test('the window going away releases the observation, and a request that never parsed starts none', async () => {
  const hub = new FakeSubscriptionPort();
  const abort = new AbortController();
  const done = subscriptionCall(hub)(REQUEST, abort.signal, () => {});
  abort.abort(new Error('gateway caller closed'));
  await done;
  expect(hub.held).toHaveLength(0);

  const gone = new AbortController();
  gone.abort();
  await subscriptionCall(hub)(REQUEST, gone.signal, () => {});
  expect(hub.held).toHaveLength(0);

  expect(() => subscriptionCall(hub)({}, new AbortController().signal, () => {})).toThrow(
    'protobuf bytes',
  );
  expect(() =>
    subscriptionCall(hub)(new Uint8Array([255]), new AbortController().signal, () => {}),
  ).toThrow();
  expect(hub.calls).toHaveLength(2);
});

test('a retry asks the hub to retry that host and is over as it is asked; it opens no observation', async () => {
  const hub = new FakeSubscriptionPort();
  const sent: Uint8Array[] = [];
  await subscriptionCall(hub)(
    encodeSubscriptionCall({kind: 'retry', host: 'ssh:host-b'}),
    new AbortController().signal,
    reply => sent.push(reply),
  );
  expect(hub.retries).toEqual(['ssh:host-b']);
  expect(hub.calls).toHaveLength(0);
  expect(sent).toHaveLength(0);
});
