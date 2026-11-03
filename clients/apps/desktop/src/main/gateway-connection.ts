/**
 * One host's gateway session, kept up for as long as someone uses it. A connection dials, shakes
 * hands, and serves calls on the session; when the session is lost it dials again inside the
 * recovery policy (`state/gateway-policy.ts`), and when that gives up it is offline for good: the
 * next user gets a new connection. A call in flight when the session is lost fails with
 * UNAVAILABLE and is never re-issued, because its caller knows how far it got (a stream's
 * sequence) or whether a command may have been applied.
 */
import type {Duplex} from 'node:stream';
import {create, toBinary} from '@bufbuild/protobuf';
import {
  API_SCHEMA_HASH,
  HelloRequestSchema,
  HelloResponseSchema,
  HostService,
  validatedFromBinary,
} from '@vibesys/proto';
import * as Effect from 'effect/Effect';
import * as Exit from 'effect/Exit';
import * as Option from 'effect/Option';
import * as Scope from 'effect/Scope';
import * as Stream from 'effect/Stream';
import * as SubscriptionRef from 'effect/SubscriptionRef';
import {afterFailure, connectBudgetMs, DIAL_DEADLINE_MS} from '../state/gateway-policy.js';
import {
  failureMessage,
  GatewayFailed,
  GrpcCode,
  GrpcFailed,
  grpcCodeOf,
  type LinkFailed,
} from './errors.js';
import {type GatewaySession, openSession} from './grpc-session.js';

export const HELLO_METHOD = `/${HostService.typeName}/${HostService.method.hello.name}`;

/** How a host's gateway is reached. */
export interface GatewayDialer {
  /**
   * A duplex to a running gateway; `force` replaces the gateway's artifact first (asked after the
   * first schema mismatch). Everything it opens ends with the scope.
   */
  readonly dial: (force: boolean) => Effect.Effect<Duplex, GatewayFailed | LinkFailed, Scope.Scope>;
}

type ConnectionState =
  | {readonly _tag: 'connecting'}
  | {readonly _tag: 'ready'; readonly session: GatewaySession}
  | {readonly _tag: 'offline'; readonly reason: string}
  | {readonly _tag: 'closed'};

export interface GatewayConnection {
  /** A unary call, waiting for the session if it is being dialed. */
  readonly unary: (
    method: string,
    bytes: Uint8Array,
  ) => Effect.Effect<Uint8Array, GrpcFailed | GatewayFailed>;
  /** A call with any number of replies, handed to `receive` in order. */
  readonly stream: (
    method: string,
    bytes: Uint8Array,
    receive: (bytes: Uint8Array) => void,
  ) => Effect.Effect<void, GrpcFailed | GatewayFailed>;
  /** Whether this connection will never serve again: offline or closed. */
  readonly ended: Effect.Effect<boolean>;
}

const helloBytes = toBinary(
  HelloRequestSchema,
  create(HelloRequestSchema, {apiSchemaHash: API_SCHEMA_HASH}),
);

/** The handshake: the gateway must answer with the schema hash this client was built with. */
const hello = (session: GatewaySession): Effect.Effect<void, GrpcFailed> =>
  session.unary(HELLO_METHOD, helloBytes).pipe(
    Effect.flatMap(bytes =>
      Effect.try({
        try: () => validatedFromBinary(HelloResponseSchema, bytes),
        catch: cause =>
          new GrpcFailed({
            code: GrpcCode.DATA_LOSS,
            message: `invalid Hello response: ${String(cause)}`,
          }),
      }),
    ),
    Effect.flatMap(response =>
      response.apiSchemaHash === API_SCHEMA_HASH
        ? Effect.void
        : Effect.fail(
            new GrpcFailed({
              code: GrpcCode.FAILED_PRECONDITION,
              message: `API hash mismatch: client ${API_SCHEMA_HASH}, gateway ${response.apiSchemaHash}`,
            }),
          ),
    ),
  );

interface Opened {
  readonly session: GatewaySession;
  /** Ends the session and what dialed it. */
  readonly scope: Scope.Closeable;
}

type DialError = GrpcFailed | GatewayFailed | LinkFailed;

/** Dial, open the session and shake hands; on any failure nothing stays open. */
const openOnce = (dialer: GatewayDialer, force: boolean): Effect.Effect<Opened, DialError> =>
  Effect.gen(function* () {
    const scope = yield* Scope.make();
    return yield* Effect.gen(function* () {
      const duplex = yield* dialer.dial(force);
      const session = yield* openSession(duplex);
      yield* hello(session);
      return {session, scope} satisfies Opened;
    }).pipe(
      Effect.provideService(Scope.Scope, scope),
      Effect.onError(() => Scope.close(scope, Exit.void)),
    );
  });

/** One dial with the one forced retry a schema mismatch earns. */
const open = (dialer: GatewayDialer): Effect.Effect<Opened, DialError> =>
  openOnce(dialer, false).pipe(
    Effect.catchIf(
      error => error._tag === 'GrpcFailed' && error.code === GrpcCode.FAILED_PRECONDITION,
      () => openOnce(dialer, true),
    ),
  );

type Connected =
  | {readonly _tag: 'opened'; readonly opened: Opened}
  | {readonly _tag: 'offline'; readonly reason: string};

/** Dial until the session is open or the policy gives up. */
const connect = (dialer: GatewayDialer, log: (line: string) => void): Effect.Effect<Connected> =>
  Effect.gen(function* () {
    let failures = 0;
    while (true) {
      const dialed = yield* open(dialer).pipe(
        Effect.timeoutOption(`${DIAL_DEADLINE_MS} millis`),
        Effect.result,
      );
      if (dialed._tag === 'Success' && Option.isSome(dialed.success))
        return {_tag: 'opened', opened: dialed.success.value} as const;
      const reason =
        dialed._tag === 'Failure' ? failureMessage(dialed.failure) : 'gateway dial timed out';
      failures += 1;
      const verdict = afterFailure(
        failures,
        dialed._tag === 'Failure' ? grpcCodeOf(dialed.failure) : undefined,
      );
      log(`gateway dial ${failures} failed: ${reason}`);
      if (verdict._tag === 'offline') return {_tag: 'offline', reason} as const;
      yield* Effect.sleep(`${verdict.afterMs} millis`);
    }
  });

const lostError = new GrpcFailed({
  code: GrpcCode.UNAVAILABLE,
  message: 'gateway connection lost',
});

/** A connection that dials at once and ends with the scope it is made in. */
export const makeGatewayConnection = (
  dialer: GatewayDialer,
  log: (line: string) => void,
): Effect.Effect<GatewayConnection, never, Scope.Scope> =>
  Effect.gen(function* () {
    const state = yield* SubscriptionRef.make<ConnectionState>({_tag: 'connecting'});
    const set = (next: ConnectionState) => SubscriptionRef.set(state, next);

    const serve = Effect.gen(function* () {
      let provisioned = false;
      while (true) {
        const connected = yield* connect(dialer, log).pipe(
          Effect.timeoutOption(`${connectBudgetMs(provisioned)} millis`),
        );
        if (Option.isNone(connected))
          return yield* set({_tag: 'offline', reason: 'gateway did not connect in time'});
        if (connected.value._tag === 'offline')
          return yield* set({_tag: 'offline', reason: connected.value.reason});
        const {session, scope} = connected.value.opened;
        provisioned = true;
        yield* set({_tag: 'ready', session});
        yield* session.lost.pipe(Effect.ensuring(Scope.close(scope, Exit.void)));
        log('gateway session lost');
        yield* set({_tag: 'connecting'});
      }
    });
    yield* Effect.forkScoped(serve);
    yield* Effect.addFinalizer(() => set({_tag: 'closed'}));

    const ready = SubscriptionRef.changes(state).pipe(
      Stream.filter(current => current._tag !== 'connecting'),
      Stream.runHead,
      Effect.flatMap(
        Option.match({
          onNone: () => Effect.fail(new GatewayFailed({message: 'gateway closed'})),
          onSome: current =>
            current._tag === 'ready'
              ? Effect.succeed(current.session)
              : Effect.fail(
                  new GatewayFailed({
                    message: current._tag === 'offline' ? current.reason : 'gateway closed',
                  }),
                ),
        }),
      ),
    );

    /** `run` on the ready session, ended when that session is lost. */
    const onSession = <A>(run: (session: GatewaySession) => Effect.Effect<A, GrpcFailed>) =>
      Effect.flatMap(ready, session =>
        Effect.raceFirst(run(session), Effect.andThen(session.lost, Effect.fail(lostError))),
      );

    return {
      unary: (method, bytes) => onSession(session => session.unary(method, bytes)),
      stream: (method, bytes, receive) =>
        onSession(session => session.call(method, bytes, receive)),
      ended: SubscriptionRef.get(state).pipe(
        Effect.map(current => current._tag === 'offline' || current._tag === 'closed'),
      ),
    } satisfies GatewayConnection;
  });
