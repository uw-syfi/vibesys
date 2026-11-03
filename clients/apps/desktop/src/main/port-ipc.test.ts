import {expect, it} from '@effect/vitest';
import * as Schema from 'effect/Schema';
import {decodeGatewayReply, gatewayReply} from '../core/gateway-codec.js';
import {GatewayFailed, GrpcFailed, LinkFailed} from './errors.js';
import {
  endOf,
  isOwnWindow,
  type PortEnd,
  type PortEvent,
  type PortFrame,
  type PortIpc,
  type PortSender,
  servePort,
} from './port-ipc.js';

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
  readonly id: number = 11;
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

it('a call is answered on its port, and ends with exactly one terminal reply and a closed port', async () => {
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

it('the page closing its port, writing to it, being destroyed or navigating away cancels the call once', async () => {
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

it('a navigation inside the page or in a frame of it does not cancel the call', async () => {
  const world = served();
  world.open();
  await settle();
  world.sender.navigate(true, true);
  world.sender.navigate(false, false);
  expect(world.log.calls[0]?.signal.aborted).toBe(false);
  expect(ends(world.port)).toEqual([]);
});

it('a call from a document the app does not own is refused before it runs', async () => {
  const refusedByAdmission = served(() => false);
  refusedByAdmission.open();
  await settle();
  expect(refusedByAdmission.log.calls).toHaveLength(0);
  expect(ends(refusedByAdmission.port)).toEqual([{error: 'Error: not an app-owned window'}]);

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

it('a call that fails ends with its failure, and a message with no port is ignored', async () => {
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

const OWN_ID = 11;
const APP_URL = 'app://vibesys/';
const own = {
  has: (id: number) => id === OWN_ID,
  isAppUrl: (url: string) => url.startsWith(APP_URL),
};

it.prop(
  'a call is from an own window only when it is the main frame of a window the app created, on an app page',
  {
    senderId: Schema.Int.check(Schema.isBetween({minimum: 10, maximum: 12})),
    isMainFrame: Schema.Boolean,
    hasFrame: Schema.Boolean,
    page: Schema.Literals(['index.html', 'https://example.test/']),
  },
  ({senderId, isMainFrame, hasFrame, page}) => {
    const frame: PortFrame = {
      processId: 1,
      routingId: 2,
      url: page.startsWith('https') ? page : `${APP_URL}${page}`,
    };
    const sender = new (class extends FakeSender {
      override readonly id = senderId;
      override readonly mainFrame = isMainFrame ? frame : {processId: 9, routingId: 9, url: 'x'};
    })();
    const event: PortEvent = {ports: [], sender, senderFrame: hasFrame ? frame : null};
    expect(isOwnWindow(event, own)).toBe(
      hasFrame && isMainFrame && senderId === OWN_ID && page === 'index.html',
    );
  },
);

it('terminal replies keep every gRPC status and a single error prefix', () => {
  const ended = (error?: unknown) => {
    const reply = decodeGatewayReply(endOf(error));
    if (reply.case !== 'end') throw new Error('not an end');
    return {error: reply.value.error, status: reply.value.status};
  };
  for (let code = 0; code <= 16; code++)
    expect(ended(new GrpcFailed({code, message: 'missing run'}))).toEqual({
      error: 'missing run',
      status: code,
    });
  expect(ended(new GatewayFailed({message: 'host unreachable'}))).toEqual({
    error: 'host unreachable',
    status: undefined,
  });
  expect(
    ended(new LinkFailed({problem: 'auth', message: 'ssh host-a: Permission denied'})),
  ).toEqual({
    error: 'ssh host-a: Permission denied',
    status: undefined,
  });
  expect(ended(new Error('caller cancelled'))).toEqual({
    error: 'Error: caller cancelled',
    status: undefined,
  });
  expect(ended()).toEqual({error: undefined, status: undefined});
});
