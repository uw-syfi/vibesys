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
import {afterFailure} from '../state/gateway-policy.js';
import {afterWindow, LIVENESS_WINDOW_MS} from '../state/liveness.js';
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
   * first schema mismatch). Everything it opens ends with the scope. The connection puts no bound
   * on a dial as a whole, so a dial bounds each phase itself (`state/gateway-policy.ts`) and
   * measures a copy by its progress.
   */
  readonly dial: (force: boolean) => Effect.Effect<Duplex, GatewayFailed | LinkFailed, Scope.Scope>;
}

type ConnectionState =
  | {readonly _tag: 'connecting'}
  | {readonly _tag: 'ready'; readonly session: GatewaySession}
  | {readonly _tag: 'offline'; readonly error: DialError}
  | {readonly _tag: 'closed'};

export interface GatewayConnection {
  /** A unary call, waiting for the session if it is being dialed. */
  readonly unary: (method: string, bytes: Uint8Array) => Effect.Effect<Uint8Array, DialError>;
  /** A call with any number of replies, handed to `receive` in order. */
  readonly stream: (
    method: string,
    bytes: Uint8Array,
    receive: (bytes: Uint8Array) => void,
  ) => Effect.Effect<void, DialError>;
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

export type SessionOpener = (
  duplex: Duplex,
) => Effect.Effect<GatewaySession, GrpcFailed, Scope.Scope>;

type DialError = GrpcFailed | GatewayFailed | LinkFailed;

/** Dial, open the session and shake hands; on any failure nothing stays open. */
const openOnce = (
  dialer: GatewayDialer,
  force: boolean,
  openSessionWith: SessionOpener,
): Effect.Effect<Opened, DialError> =>
  Effect.gen(function* () {
    const scope = yield* Scope.make();
    return yield* Effect.gen(function* () {
      const duplex = yield* dialer.dial(force);
      const session = yield* openSessionWith(duplex);
      yield* hello(session);
      return {session, scope} satisfies Opened;
    }).pipe(
      Effect.provideService(Scope.Scope, scope),
      Effect.onError(() => Scope.close(scope, Exit.void)),
    );
  });

/** One dial with the one forced retry a schema mismatch earns. */
const open = (
  dialer: GatewayDialer,
  openSessionWith: SessionOpener,
): Effect.Effect<Opened, DialError> =>
  openOnce(dialer, false, openSessionWith).pipe(
    Effect.catchIf(
      error => error._tag === 'GrpcFailed' && error.code === GrpcCode.FAILED_PRECONDITION,
      () => openOnce(dialer, true, openSessionWith),
    ),
  );

type Connected =
  | {readonly _tag: 'opened'; readonly opened: Opened}
  | {readonly _tag: 'offline'; readonly error: DialError};

const fatalAuth = (error: DialError): boolean =>
  error._tag === 'LinkFailed' && error.problem === 'auth';

/**
 * Dial until the session is open or the policy gives up. A dial has no bound of its own: each of
 * its phases is bounded by what it waits for (`state/gateway-policy.ts`), and the copy of the host
 * program by its progress, so a slow copy that keeps moving finishes.
 */
const connect = (
  dialer: GatewayDialer,
  log: (line: string) => void,
  openSessionWith: SessionOpener,
): Effect.Effect<Connected> =>
  Effect.gen(function* () {
    let failures = 0;
    while (true) {
      const dialed = yield* open(dialer, openSessionWith).pipe(Effect.result);
      if (dialed._tag === 'Success') return {_tag: 'opened', opened: dialed.success} as const;
      const error = dialed.failure;
      const reason = failureMessage(error);
      failures += 1;
      const verdict = afterFailure(failures, grpcCodeOf(error));
      log(`gateway dial ${failures} failed: ${reason}`);
      if (fatalAuth(error) || verdict._tag === 'offline') return {_tag: 'offline', error} as const;
      yield* Effect.sleep(`${verdict.afterMs} millis`);
    }
  });

/**
 * Ends when `session` delivered nothing for two windows in a row. The first silent window ends
 * with a probe (the handshake again, a request whose reply is bytes), so a quiet link that is
 * alive keeps answering and one that is dead does not. Bytes arriving at all, however slowly, end
 * the silence: this never cuts a transfer for being slow.
 */
const silent = (session: GatewaySession): Effect.Effect<void> =>
  Effect.scoped(
    Effect.gen(function* () {
      let seen = yield* session.activity;
      let quiet = 0;
      while (true) {
        yield* Effect.sleep(`${LIVENESS_WINDOW_MS} millis`);
        const now = yield* session.activity;
        const step = afterWindow(now !== seen, quiet);
        seen = now;
        quiet = step.silent;
        if (step.action === 'lost') return;
        // The probe's own failure is not the verdict: only silence is.
        if (step.action === 'probe') yield* Effect.forkScoped(Effect.ignore(hello(session)));
      }
    }),
  );

const lostError = new GrpcFailed({
  code: GrpcCode.UNAVAILABLE,
  message: 'gateway connection lost',
});

/** A connection that dials at once and ends with the scope it is made in. */
export const makeGatewayConnection = (
  dialer: GatewayDialer,
  log: (line: string) => void,
  openSessionWith: SessionOpener = openSession,
): Effect.Effect<GatewayConnection, never, Scope.Scope> =>
  Effect.gen(function* () {
    const state = yield* SubscriptionRef.make<ConnectionState>({_tag: 'connecting'});
    const set = (next: ConnectionState) => SubscriptionRef.set(state, next);

    const serve = Effect.gen(function* () {
      while (true) {
        const connected = yield* connect(dialer, log, openSessionWith);
        if (connected._tag === 'offline')
          return yield* set({_tag: 'offline', error: connected.error});
        const {session, scope} = connected.opened;
        yield* set({_tag: 'ready', session});
        // Closing the scope ends the session, which fails the calls still on it as UNAVAILABLE.
        const ended = yield* Effect.raceFirst(
          Effect.as(session.lost, 'lost'),
          Effect.as(silent(session), 'silent'),
        ).pipe(Effect.ensuring(Scope.close(scope, Exit.void)));
        log(ended === 'silent' ? 'gateway link went silent' : 'gateway session lost');
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
                  current._tag === 'offline'
                    ? current.error
                    : new GatewayFailed({message: 'gateway closed'}),
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
