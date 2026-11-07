import type {Duplex} from 'node:stream';
import {create, toBinary} from '@bufbuild/protobuf';
import {describe, expect, it} from '@effect/vitest';
import {ListItemsRequestSchema, ListItemsResponseSchema} from '@vibesys/orchestrator-proto';
import {DesktopRefusal} from '@vibesys/proto';
import * as Effect from 'effect/Effect';
import * as Exit from 'effect/Exit';
import * as Fiber from 'effect/Fiber';
import * as Scope from 'effect/Scope';
import * as TestClock from 'effect/testing/TestClock';
import {CONNECTION_LINGER_MS} from '../state/gateway-policy.js';
import {type HostKey, hostKey} from '../state/host.js';
import {untilTrue} from '../testing/main/drive.js';
import {fakeGateway} from '../testing/main/fake-gateway.js';
import {GatewayFailed, GatewayRefused, GrpcFailed} from './errors.js';
import {type GatewayCalls, makeGatewayCalls} from './gateway-calls.js';
import type {GatewayDialer} from './gateway-connection.js';

const HOST_A = hostKey({kind: 'ssh', alias: 'host-a'});
const HOST_B = hostKey({kind: 'ssh', alias: 'host-b'});
const LIST_ITEMS = '/vibesys.orchestrator.v1.OrchestratorService/ListItems';
const request = toBinary(
  ListItemsRequestSchema,
  create(ListItemsRequestSchema, {agentId: 'agent-1', beforeOrdinal: 1, pageSize: 10}),
);
const reply = toBinary(ListItemsResponseSchema, create(ListItemsResponseSchema, {page: {}}));

/** A world of fake gateways, one per host, and a record of the dials. */
function world(options: {failing?: ReadonlySet<HostKey>} = {}) {
  const peers = new Map<HostKey, ReturnType<typeof fakeGateway>>();
  const dials: HostKey[] = [];
  const peer = (host: HostKey) => {
    let found = peers.get(host);
    if (found === undefined) {
      found = fakeGateway();
      peers.set(host, found);
    }
    return found;
  };
  const dialerFor = (host: HostKey): GatewayDialer => ({
    dial: () =>
      Effect.gen(function* () {
        dials.push(host);
        if (options.failing?.has(host) === true)
          return yield* new GatewayFailed({message: 'host unreachable'});
        const duplex: Duplex = peer(host).connect();
        yield* Effect.addFinalizer(() => Effect.sync(() => duplex.destroy()));
        return duplex;
      }),
  });
  return {peer, dials, dialerFor, lines: [] as string[]};
}

const calls = (w: ReturnType<typeof world>) => {
  const scope = Scope.makeUnsafe();
  return makeGatewayCalls(w.dialerFor, (host, line) => w.lines.push(`${host}: ${line}`)).pipe(
    Effect.provideService(Scope.Scope, scope),
    Effect.map(made => ({made, scope})),
  );
};

describe('gateway calls', () => {
  it.effect('answers a unary call and a stream over one connection per host', () =>
    Effect.gen(function* () {
      const w = world();
      const {made, scope} = yield* calls(w);
      const unary = yield* Effect.forkChild(made.unary(HOST_A, LIST_ITEMS, request));
      const heard: Uint8Array[] = [];
      const stream = yield* Effect.forkChild(
        made.stream(HOST_A, LIST_ITEMS, request, bytes => heard.push(bytes)),
      );
      yield* untilTrue(() => (w.peer(HOST_A).calls.length ?? 0) === 2);
      for (const call of w.peer(HOST_A).calls) {
        call.send(reply);
        call.end();
      }
      expect((yield* Fiber.join(unary)).byteLength).toBe(reply.byteLength);
      yield* Fiber.join(stream);
      expect(heard).toHaveLength(1);
      expect(w.dials).toEqual([HOST_A]);
      yield* Scope.close(scope, Exit.void);
    }),
  );

  /** One unary call on `HOST_A`, answered; `round` counts the calls the peer has had. */
  const answered = (w: ReturnType<typeof world>, made: GatewayCalls, round: number) =>
    Effect.gen(function* () {
      const call = yield* Effect.forkChild(made.unary(HOST_A, LIST_ITEMS, request));
      yield* untilTrue(() => w.peer(HOST_A).calls.length === round);
      w.peer(HOST_A).calls[round - 1]?.send(reply);
      w.peer(HOST_A).calls[round - 1]?.end();
      yield* Fiber.join(call);
    });

  it.effect(
    'keeps a connection for the linger after its last call, so the next call finds it',
    () =>
      Effect.gen(function* () {
        const w = world();
        const {made, scope} = yield* calls(w);
        yield* answered(w, made, 1);
        // A call within the linger is on the connection the first dialed: one dial, not two.
        yield* TestClock.adjust(`${CONNECTION_LINGER_MS - 1} millis`);
        yield* answered(w, made, 2);
        expect(w.dials).toEqual([HOST_A]);
        yield* Scope.close(scope, Exit.void);
      }),
  );

  it.effect('closes a connection a linger after its last call, and dials again for the next', () =>
    Effect.gen(function* () {
      const w = world();
      const {made, scope} = yield* calls(w);
      yield* answered(w, made, 1);
      yield* TestClock.adjust(`${CONNECTION_LINGER_MS} millis`);
      yield* Effect.yieldNow;
      yield* answered(w, made, 2);
      expect(w.dials).toEqual([HOST_A, HOST_A]);
      yield* Scope.close(scope, Exit.void);
    }),
  );

  it.effect('counts the linger from the last call to end, however many came before it', () =>
    Effect.gen(function* () {
      const w = world();
      const {made, scope} = yield* calls(w);
      // Calls ending a little before each linger runs out keep the connection for as long as they come.
      for (const round of [1, 2, 3, 4]) {
        yield* answered(w, made, round);
        yield* TestClock.adjust(`${CONNECTION_LINGER_MS - 1} millis`);
      }
      expect(w.dials).toEqual([HOST_A]);
      yield* Scope.close(scope, Exit.void);
    }),
  );

  it.effect('keeps hosts apart', () =>
    Effect.gen(function* () {
      const w = world();
      const {made, scope} = yield* calls(w);
      const a = yield* Effect.forkChild(made.unary(HOST_A, LIST_ITEMS, request));
      const b = yield* Effect.forkChild(made.unary(HOST_B, LIST_ITEMS, request));
      yield* untilTrue(
        () => w.peer(HOST_A).calls.length === 1 && w.peer(HOST_B).calls.length === 1,
      );
      for (const host of [HOST_A, HOST_B]) {
        w.peer(host).calls[0]?.send(reply);
        w.peer(host).calls[0]?.end();
      }
      yield* Fiber.join(a);
      yield* Fiber.join(b);
      expect([...w.dials].sort()).toEqual([HOST_A, HOST_B].sort());
      yield* Scope.close(scope, Exit.void);
    }),
  );

  it.effect(
    "refuses a method outside the app's, and a payload its schema rejects, before dialing",
    () =>
      Effect.gen(function* () {
        const w = world();
        const {made, scope} = yield* calls(w);
        const unknown = yield* Effect.flip(
          made.unary(HOST_A, '/vibesys.host.v1.HostService/Tail', request),
        );
        expect(unknown).toMatchObject({
          _tag: 'GatewayRefused',
          refusal: DesktopRefusal.METHOD_NOT_AVAILABLE,
        });
        expect(unknown.message).toContain('method not available');
        const invalid = yield* Effect.flip(made.unary(HOST_A, LIST_ITEMS, new Uint8Array([0xff])));
        expect(invalid).toMatchObject({
          _tag: 'GatewayRefused',
          refusal: DesktopRefusal.INVALID_REQUEST,
        });
        expect(w.dials).toEqual([]);
        yield* Scope.close(scope, Exit.void);
      }),
  );

  it.effect('ends a call whose reply breaks the schema with DATA_LOSS', () =>
    Effect.gen(function* () {
      const w = world();
      const {made, scope} = yield* calls(w);
      const call = yield* Effect.forkChild(Effect.flip(made.unary(HOST_A, LIST_ITEMS, request)));
      yield* untilTrue(() => w.peer(HOST_A).calls.length === 1);
      w.peer(HOST_A).calls[0]?.send(new Uint8Array([0xff]));
      w.peer(HOST_A).calls[0]?.end();
      const failure = yield* Fiber.join(call);
      expect(failure).toBeInstanceOf(GrpcFailed);
      expect((failure as GrpcFailed).code).toBe(15);
      yield* Scope.close(scope, Exit.void);
    }),
  );

  it.effect('gives a call on an offline connection a new one next time, and logs the failure', () =>
    Effect.gen(function* () {
      const w = world({failing: new Set([HOST_A])});
      const {made, scope} = yield* calls(w);
      const first = yield* Effect.forkChild(Effect.flip(made.unary(HOST_A, LIST_ITEMS, request)));
      yield* TestClock.adjust('8 seconds');
      const failure = yield* Fiber.join(first);
      expect(failure.message).toBe('host unreachable');
      expect(w.dials).toHaveLength(5);
      const second = yield* Effect.forkChild(Effect.flip(made.unary(HOST_A, LIST_ITEMS, request)));
      yield* TestClock.adjust('8 seconds');
      yield* Fiber.join(second);
      expect(w.dials).toHaveLength(10);
      expect(
        w.lines.some(line => line.includes('gateway call failed') && line.includes('no status')),
      ).toBe(true);
      yield* Scope.close(scope, Exit.void);
    }),
  );

  it.effect(
    'starts a connection ahead of the first call and drops it when unused for the hold',
    () =>
      Effect.gen(function* () {
        const w = world();
        const {made, scope} = yield* calls(w);
        yield* made.warm(HOST_A, '15 seconds');
        yield* untilTrue(() => w.dials.length === 1);
        // A call within the hold finds the connection ready: no second dial.
        const call = yield* Effect.forkChild(made.unary(HOST_A, LIST_ITEMS, request));
        yield* untilTrue(() => w.peer(HOST_A).calls.length === 1);
        w.peer(HOST_A).calls[0]?.send(reply);
        w.peer(HOST_A).calls[0]?.end();
        yield* Fiber.join(call);
        expect(w.dials).toEqual([HOST_A]);
        yield* TestClock.adjust('15 seconds');
        yield* Scope.close(scope, Exit.void);
      }),
  );

  it.effect('refuses calls once closed', () =>
    Effect.gen(function* () {
      const w = world();
      const {made, scope} = yield* calls(w);
      yield* Scope.close(scope, Exit.void);
      const failure = yield* Effect.flip(made.unary(HOST_A, LIST_ITEMS, request));
      expect(failure.message).toBe('gateway bridge closed');
    }),
  );
});

describe('a call the app refuses to make', () => {
  it.effect('is dialed once and never again: no redial changes a refusal', () =>
    Effect.gen(function* () {
      const dials: HostKey[] = [];
      const refusing = (host: HostKey) => ({
        dial: () =>
          Effect.gen(function* () {
            dials.push(host);
            return yield* new GatewayRefused({
              refusal: DesktopRefusal.UNSUPPORTED_PLATFORM,
              message: 'unsupported gateway platform: Linux riscv64',
            });
          }),
      });
      const scope = Scope.makeUnsafe();
      const made = yield* makeGatewayCalls(refusing, () => {}).pipe(
        Effect.provideService(Scope.Scope, scope),
      );
      const failure = yield* Effect.flip(made.unary(HOST_A, LIST_ITEMS, request));
      expect(failure).toMatchObject({_tag: 'GatewayRefused'});
      // Not five dials with a wait between them, as for a link that may come back.
      expect(dials).toEqual([HOST_A]);
      yield* Scope.close(scope, Exit.void);
    }),
  );
});
