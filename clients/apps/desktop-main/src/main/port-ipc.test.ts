import {expect, test} from 'bun:test';
import {create} from '@bufbuild/protobuf';
import {timestampFromMs} from '@bufbuild/protobuf/wkt';
import {SUBSCRIPTION_LINGER_MS, Subscriptions} from '@vibesys/connection';
import {type FakeSubscribeStream, FakeSubscribeStreams} from '@vibesys/connection/testing';
import {
  decodeGatewayReply,
  type GatewayPortBridge,
  gatewayReply,
  SUBSCRIPTION_CHANNEL,
  subscriptionBridge,
} from '@vibesys/ipc';
import {MachineStatsSchema, TopicEntrySchema} from '@vibesys/proto';
import {machineStats, type SubscriptionState, subscriptionSource} from '@vibesys/subscription';
import {FakeClock} from '@vibesys/transport/testing';
import {
  type PortEnd,
  type PortEvent,
  type PortFrame,
  type PortIpc,
  type PortSender,
  servePort,
} from './port-ipc.js';
import {installSubscriptionIpc} from './subscription-ipc.js';

const ENTRY = create(TopicEntrySchema, {
  key: 'machine',
  value: {
    case: 'machineStats',
    value: create(MachineStatsSchema, {
      sampledAt: timestampFromMs(1000),
      cpu: {case: 'cpuUnavailable', value: {reason: 1}},
      memory: {case: 'memoryUnavailable', value: {reason: 1}},
      gpu: {case: 'gpuUnavailable', value: {reason: 1}},
    }),
  },
});

const FRAME: PortFrame = {processId: 7, routingId: 3, url: 'app://vibesys/shell.html'};

/** The main side of a port, as Electron has it: a close fires 'close', a post reaches the page. */
class FakePort implements PortEnd {
  readonly posted: Uint8Array[] = [];
  closes = 0;
  started = 0;
  readonly #listeners = {message: [] as (() => void)[], close: [] as (() => void)[]};
  /** What the page receives. */
  toPage: (message: Uint8Array) => void = () => {};

  on(event: 'message' | 'close', listener: () => void): void {
    this.#listeners[event].push(listener);
  }
  start(): void {
    this.started += 1;
  }
  postMessage(message: Uint8Array): void {
    this.posted.push(message);
    this.toPage(message);
  }
  close(): void {
    this.closes += 1;
    this.fromPage('close');
  }
  /** The page does something on its end of the port. */
  fromPage(event: 'message' | 'close'): void {
    for (const listener of this.#listeners[event]) listener();
  }
}

class FakeSender implements PortSender {
  readonly id = 11;
  readonly mainFrame = FRAME;
  readonly #destroyed = new Set<() => void>();
  readonly #navigation = new Set<
    (event: unknown, url: string, inPage: boolean, mainFrame: boolean) => void
  >();

  once(_event: 'destroyed', listener: () => void): void {
    this.#destroyed.add(listener);
  }
  on(
    _event: 'did-start-navigation',
    listener: (event: unknown, url: string, inPage: boolean, mainFrame: boolean) => void,
  ): void {
    this.#navigation.add(listener);
  }
  removeListener(
    event: 'destroyed' | 'did-start-navigation',
    listener: (...args: never[]) => void,
  ) {
    (event === 'destroyed' ? this.#destroyed : this.#navigation).delete(listener as never);
  }
  get listening(): number {
    return this.#destroyed.size + this.#navigation.size;
  }
  destroy(): void {
    for (const listener of [...this.#destroyed]) listener();
  }
  navigate(inPage: boolean, mainFrame: boolean): void {
    for (const listener of [...this.#navigation]) listener({}, 'app://x', inPage, mainFrame);
  }
}

class FakeIpc implements PortIpc {
  readonly #listeners = new Map<string, (event: PortEvent, value: unknown) => void>();
  on(channel: string, listener: (event: PortEvent, value: unknown) => void): void {
    this.#listeners.set(channel, listener);
  }
  sendWithoutPort(channel: string, value: unknown, sender: PortSender): void {
    this.#listeners.get(channel)?.({ports: [], sender, senderFrame: FRAME}, value);
  }
  /** A page sends `value` on `channel`, with a port to be answered on. */
  send(channel: string, value: unknown, port: PortEnd, sender: PortSender, frame = FRAME): void {
    const listener = this.#listeners.get(channel);
    if (listener === undefined) throw new Error(`nothing serves ${channel}`);
    listener({ports: [port], sender, senderFrame: frame}, value);
  }
}

async function settle(): Promise<void> {
  for (let drain = 0; drain < 5; drain += 1) await Promise.resolve();
}

function ends(port: FakePort): {error: string | undefined}[] {
  return port.posted.flatMap(message => {
    const reply = decodeGatewayReply(message);
    return reply.case === 'end' ? [{error: reply.value.error}] : [];
  });
}

interface Served {
  readonly calls: {value: unknown; signal: AbortSignal; send(reply: Uint8Array): void}[];
  readonly settle: {resolve(): void; reject(error: Error): void}[];
}

function served(admits: () => boolean = () => true) {
  const ipc = new FakeIpc();
  const sender = new FakeSender();
  const log: Served = {calls: [], settle: []};
  servePort(ipc, 'vibesys:test', admits, (value, signal, send) => {
    log.calls.push({value, signal, send});
    return new Promise<void>((resolve, reject) => log.settle.push({resolve, reject}));
  });
  const port = new FakePort();
  const open = (frame = FRAME): void => ipc.send('vibesys:test', 'request', port, sender, frame);
  return {ipc, sender, port, log, open};
}

test('a call is answered on its port, and ends with exactly one terminal reply and a closed port', async () => {
  const {port, log, open, sender} = served();
  open();
  expect(port.started).toBe(1);
  expect(sender.listening).toBe(2);
  await settle();
  expect(log.calls[0]?.value).toBe('request');
  log.calls[0]?.send(gatewayReply(new Uint8Array([1])));
  expect(port.posted).toHaveLength(1);
  log.settle[0]?.resolve();
  await settle();
  expect(ends(port)).toEqual([{error: undefined}]);
  expect(port.closes).toBe(1);
  expect(log.calls[0]?.signal.aborted).toBe(true);
  expect(sender.listening).toBe(0);
  // Nothing after the end reaches the page.
  log.calls[0]?.send(gatewayReply(new Uint8Array([2])));
  port.fromPage('close');
  expect(port.posted).toHaveLength(2);
});

test('the page closing its port, writing to it, being destroyed or navigating away cancels the call once', async () => {
  const ways: ((world: ReturnType<typeof served>) => void)[] = [
    world => world.port.fromPage('close'),
    world => world.port.fromPage('message'),
    world => world.sender.destroy(),
    world => world.sender.navigate(false, true),
  ];
  for (const leave of ways) {
    const world = served();
    world.open();
    await settle();
    leave(world);
    leave(world);
    expect(world.log.calls[0]?.signal.aborted).toBe(true);
    expect(ends(world.port)).toEqual([{error: 'Error: gateway caller closed'}]);
    expect(world.port.closes).toBe(1);
    expect(world.sender.listening).toBe(0);
  }
});

test('a navigation inside the page or in a frame of it does not cancel the call', async () => {
  const world = served();
  world.open();
  await settle();
  world.sender.navigate(true, true);
  world.sender.navigate(false, false);
  expect(world.log.calls[0]?.signal.aborted).toBe(false);
  expect(ends(world.port)).toEqual([]);
});

test('a call from a document the app does not own is refused before it runs', async () => {
  const refusedByAdmission = served(() => false);
  refusedByAdmission.open();
  await settle();
  expect(refusedByAdmission.log.calls).toHaveLength(0);
  expect(ends(refusedByAdmission.port)).toEqual([
    {error: 'Error: not an app-owned shell document'},
  ]);

  const subframe = served();
  subframe.ipc.send(
    'vibesys:test',
    'request',
    subframe.port,
    new (class extends FakeSender {
      override readonly mainFrame = {processId: 1, routingId: 1, url: 'other'};
    })(),
  );
  await settle();
  expect(subframe.log.calls).toHaveLength(0);
  expect(ends(subframe.port)).toHaveLength(1);
});

test('a call that fails ends with its failure, and a message with no port is ignored', async () => {
  const world = served();
  world.open();
  await settle();
  world.log.settle[0]?.reject(new Error('host exploded'));
  await settle();
  expect(ends(world.port)).toEqual([{error: 'Error: host exploded'}]);
  expect(world.port.closes).toBe(1);

  // A message that arrives with no port to answer on starts nothing.
  const ipc = new FakeIpc();
  let started = 0;
  servePort(
    ipc,
    'vibesys:test',
    () => true,
    async () => {
      started += 1;
    },
  );
  ipc.sendWithoutPort('vibesys:test', 'request', new FakeSender());
  expect(started).toBe(0);
});

/** The page's port to main: a window's preload over the same fakes. */
function preloadOver(ipc: FakeIpc, sender: FakeSender, ports: FakePort[]): GatewayPortBridge {
  return {
    open(request, receive) {
      const port = new FakePort();
      port.toPage = receive;
      ports.push(port);
      ipc.send(SUBSCRIPTION_CHANNEL, request, port, sender);
      return {cancel: () => port.fromPage('close')};
    },
  };
}

function composition() {
  const clock = new FakeClock();
  const gateway = new FakeSubscribeStreams();
  const hub = new Subscriptions(gateway, clock.schedule, () => clock.now());
  const ipc = new FakeIpc();
  installSubscriptionIpc(ipc, {hub: hub.port, admits: () => true});
  const sender = new FakeSender();
  const ports: FakePort[] = [];
  const source = subscriptionSource(
    subscriptionBridge(preloadOver(ipc, sender, ports)),
    'ssh:host-a',
    machineStats,
  );
  const states: SubscriptionState<unknown>[] = [];
  const stream = (): FakeSubscribeStream => {
    const open = gateway.streams.at(-1);
    if (open === undefined) throw new Error('no stream');
    return open;
  };
  return {clock, gateway, hub, ipc, sender, ports, source, states, stream};
}

test('window to port to servePort to hub to machine: live in the window, and cancelled when the window leaves', () => {
  const world = composition();
  const release = world.source.subscribe(() => world.states.push(world.source.getSnapshot()));
  expect(world.gateway.open).toHaveLength(1);
  expect(world.ports).toHaveLength(1);
  world.stream().reached('runs');
  world.stream().send({case: 'entry', value: ENTRY});
  world.stream().send({case: 'synced', value: {}});
  expect(world.source.getSnapshot().kind).toBe('live');
  release();
  // The window's last listener left: its port closed and the call was cancelled. The hub keeps the
  // stream for a page that comes straight back, then lets go.
  expect(world.ports[0]?.closes).toBe(1);
  expect(world.gateway.open).toHaveLength(1);
  world.clock.advanceBy(SUBSCRIPTION_LINGER_MS);
  expect(world.gateway.open).toHaveLength(0);
  expect(world.clock.pending).toBe(0);
});

test('removing the host reaches the window as failed closed, and closes its port', async () => {
  const world = composition();
  world.source.subscribe(() => {});
  world.stream().reached('runs');
  world.hub.closeHost('ssh:host-a');
  await settle();
  expect(world.source.getSnapshot()).toEqual({kind: 'failed', reason: 'closed', retrying: false});
  expect(world.gateway.open).toHaveLength(0);
  expect(world.ports[0]?.closes).toBe(1);
});

test('a page that main does not admit sees failed closed, and no stream opens', async () => {
  const clock = new FakeClock();
  const gateway = new FakeSubscribeStreams();
  const hub = new Subscriptions(gateway, clock.schedule, () => clock.now());
  const ipc = new FakeIpc();
  installSubscriptionIpc(ipc, {hub: hub.port, admits: () => false});
  const source = subscriptionSource(
    subscriptionBridge(preloadOver(ipc, new FakeSender(), [])),
    'local',
    machineStats,
  );
  source.subscribe(() => {});
  await settle();
  expect(source.getSnapshot()).toEqual({kind: 'failed', reason: 'closed', retrying: false});
  expect(gateway.streams).toHaveLength(0);
});
