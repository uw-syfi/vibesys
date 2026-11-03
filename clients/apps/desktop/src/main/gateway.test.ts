import {describe, expect, it} from '@effect/vitest';
import * as Effect from 'effect/Effect';
import {decodeGatewayReply, encodeGatewayRequest, GATEWAY_CHANNEL} from '../core/gateway-codec.js';
import {hostKey} from '../state/host.js';
import {GatewayFailed, GrpcFailed} from './errors.js';
import {installGatewayIpc, type RunCall} from './gateway.js';
import type {GatewayCalls} from './gateway-calls.js';
import type {PortEnd, PortEvent, PortFrame, PortIpc, PortSender} from './port-ipc.js';

const FRAME: PortFrame = {processId: 1, routingId: 1, url: 'app://vibesys/index.html'};
const HOST = hostKey({kind: 'ssh', alias: 'host-a'});
const METHOD = '/vibesys.orchestrator.v1.OrchestratorService/ListItems';

class Port implements PortEnd {
  readonly posted: Uint8Array[] = [];
  closed = false;
  readonly #listeners = {message: [] as (() => void)[], close: [] as (() => void)[]};
  on(event: 'message' | 'close', listener: () => void): void {
    this.#listeners[event].push(listener);
  }
  start(): void {}
  postMessage(message: Uint8Array): void {
    this.posted.push(message);
  }
  close(): void {
    this.closed = true;
  }
  page(event: 'message' | 'close'): void {
    for (const listener of this.#listeners[event]) listener();
  }
  /** What the page read: replies, then the end. */
  read() {
    return this.posted.map(decodeGatewayReply);
  }
}

const sender: PortSender = {
  id: 1,
  mainFrame: FRAME,
  once: () => {},
  on: () => {},
  removeListener: () => {},
};

/** Install the gateway over `calls`; returns what sends a request. */
const installed = (calls: GatewayCalls) =>
  Effect.gen(function* () {
    const services = yield* Effect.context<never>();
    const runWith = Effect.runPromiseExitWith(services);
    return install(calls, (effect, signal) => runWith(effect, {signal}));
  });

function install(calls: GatewayCalls, run: RunCall) {
  let listener: ((event: PortEvent, value: unknown) => void) | undefined;
  const ipc: PortIpc = {
    on: (channel, next) => {
      expect(channel).toBe(GATEWAY_CHANNEL);
      listener = next;
    },
  };
  installGatewayIpc(ipc, {calls, admits: () => true, run});
  return (value: unknown): Port => {
    const port = new Port();
    listener?.({ports: [port], sender, senderFrame: FRAME}, value);
    return port;
  };
}

const settled = (port: Port) =>
  Effect.promise(async () => {
    while (!port.closed) await new Promise(resolve => setImmediate(resolve));
  });

const unusedCalls: GatewayCalls = {
  warm: () => Effect.void,
  unary: () => Effect.die('unused'),
  stream: () => Effect.die('unused'),
};

describe('gateway ipc', () => {
  it.effect('answers a unary request with its reply and then a clean end', () =>
    Effect.gen(function* () {
      const send = yield* installed({
        ...unusedCalls,
        unary: (host, method, bytes) => {
          expect([host, method]).toEqual([HOST, METHOD]);
          return Effect.succeed(Uint8Array.from([...bytes, 9]));
        },
      });
      const port = send(
        encodeGatewayRequest({kind: 'unary', host: HOST, method: METHOD, bytes: Uint8Array.of(1)}),
      );
      yield* settled(port);
      const [reply, end] = port.read();
      expect(reply).toMatchObject({case: 'bytes'});
      expect(end?.case).toBe('end');
      expect((end?.value as {error?: string} | undefined)?.error).toBeUndefined();
    }),
  );

  it.effect("hands a stream's messages over in order", () =>
    Effect.gen(function* () {
      const send = yield* installed({
        ...unusedCalls,
        stream: (_host, _method, _bytes, receive) =>
          Effect.sync(() => {
            receive(Uint8Array.of(1));
            receive(Uint8Array.of(2));
          }),
      });
      const port = send(
        encodeGatewayRequest({kind: 'stream', host: HOST, method: METHOD, bytes: new Uint8Array()}),
      );
      yield* settled(port);
      expect(port.read().map(reply => reply.case)).toEqual(['bytes', 'bytes', 'end']);
    }),
  );

  it.effect('ends a refused call with its gRPC status and its words', () =>
    Effect.gen(function* () {
      const send = yield* installed({
        ...unusedCalls,
        unary: () => Effect.fail(new GrpcFailed({code: 5, message: 'no such run'})),
      });
      const port = send(
        encodeGatewayRequest({kind: 'unary', host: HOST, method: METHOD, bytes: new Uint8Array()}),
      );
      yield* settled(port);
      expect(port.read().at(-1)).toMatchObject({
        case: 'end',
        value: {error: 'no such run', status: 5},
      });
    }),
  );

  it.effect("ends a call that failed before reaching the gateway with the failure's words", () =>
    Effect.gen(function* () {
      const send = yield* installed({
        ...unusedCalls,
        unary: () => Effect.fail(new GatewayFailed({message: 'host unreachable'})),
      });
      const port = send(
        encodeGatewayRequest({kind: 'unary', host: HOST, method: METHOD, bytes: new Uint8Array()}),
      );
      yield* settled(port);
      expect(port.read().at(-1)).toMatchObject({case: 'end', value: {error: 'host unreachable'}});
    }),
  );

  it.effect('ends a request that is not a request, without calling the gateway', () =>
    Effect.gen(function* () {
      const send = yield* installed(unusedCalls);
      const port = send('nonsense');
      yield* settled(port);
      expect(port.read()).toHaveLength(1);
      expect(port.read()[0]).toMatchObject({case: 'end'});
    }),
  );

  it.effect('interrupts a call whose window closed its port', () =>
    Effect.gen(function* () {
      let interrupted = false;
      const send = yield* installed({
        ...unusedCalls,
        unary: () =>
          Effect.never.pipe(
            Effect.onInterrupt(() =>
              Effect.sync(() => {
                interrupted = true;
              }),
            ),
          ),
      });
      const port = send(
        encodeGatewayRequest({kind: 'unary', host: HOST, method: METHOD, bytes: new Uint8Array()}),
      );
      yield* Effect.promise(() => new Promise(resolve => setImmediate(resolve)));
      port.page('close');
      yield* Effect.promise(async () => {
        while (!interrupted) await new Promise(resolve => setImmediate(resolve));
      });
      expect(interrupted).toBe(true);
    }),
  );
});
