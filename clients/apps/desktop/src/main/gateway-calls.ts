/**
 * The calls the window makes, answered over one gateway connection per host. A connection is
 * shared by the calls using it and closed when the last one ends (or, for one started ahead with
 * `warm`, when its hold runs out unused). An offline connection is replaced by a new one on the
 * next call.
 *
 * The window may call only what the app uses: the handshake and the orchestrator's methods. Every
 * payload is checked against the generated schemas before it is forwarded and again before it is
 * handed on.
 */
import {HostControlService, OrchestratorService} from '@vibesys/orchestrator-proto';
import {DesktopRefusal, HostService, validateServicePayload} from '@vibesys/proto';
import type * as Duration from 'effect/Duration';
import * as Effect from 'effect/Effect';
import * as Exit from 'effect/Exit';
import * as Scope from 'effect/Scope';
import {CONNECTION_LINGER_MS} from '../state/gateway-policy.js';
import type {HostKey} from '../state/host.js';
import {
  failureMessage,
  GatewayFailed,
  GatewayRefused,
  GrpcCode,
  GrpcFailed,
  grpcCodeOf,
  type LinkFailed,
} from './errors.js';
import {
  type GatewayConnection,
  type GatewayDialer,
  HELLO_METHOD,
  makeGatewayConnection,
} from './gateway-connection.js';
import {type CallMetadata, openSession} from './grpc-session.js';

/** The services a payload is checked against. */
const SERVICES = [HostService, OrchestratorService, HostControlService];

/**
 * The methods the window may call: the handshake, every orchestrator method, and the host's
 * `Identify`, which says why a host does not serve. Never `Stop`: the app does not end hosts.
 */
const ALLOWED: ReadonlySet<string> = new Set([
  HELLO_METHOD,
  ...OrchestratorService.methods.map(method => `/${OrchestratorService.typeName}/${method.name}`),
  `/${HostControlService.typeName}/${HostControlService.method.identify.name}`,
]);

export type CallError = GrpcFailed | GatewayFailed | GatewayRefused | LinkFailed;

export interface GatewayCalls {
  /**
   * Start the connection to `host` now, ahead of its first call, so that what the host needs to
   * start (a process to exec, a handshake) runs beside whatever the caller is still doing. A call
   * within `hold` finds it ready or on its way; unused for `hold`, it is closed.
   */
  readonly warm: (host: HostKey, hold: Duration.Input) => Effect.Effect<void>;
  readonly unary: (
    host: HostKey,
    method: string,
    bytes: Uint8Array,
  ) => Effect.Effect<Uint8Array, CallError>;
  readonly stream: (
    host: HostKey,
    method: string,
    bytes: Uint8Array,
    receive: (bytes: Uint8Array) => void,
  ) => Effect.Effect<void, CallError>;
}

interface Entry {
  readonly connection: GatewayConnection;
  readonly scope: Scope.Closeable;
  users: number;
  /** Counts the times the connection went unused, so a linger timer knows whether it is current. */
  idles: number;
}

/** A call the app will not make is refused for good: no dial or retry changes it. */
const checkInput = (method: string, bytes: Uint8Array): Effect.Effect<void, GatewayRefused> =>
  Effect.gen(function* () {
    if (!ALLOWED.has(method))
      return yield* new GatewayRefused({
        refusal: DesktopRefusal.METHOD_NOT_AVAILABLE,
        message: `method not available: ${method}`,
      });
    yield* Effect.try({
      try: () => validateServicePayload(SERVICES, method, 'input', bytes),
      catch: cause =>
        new GatewayRefused({
          refusal: DesktopRefusal.INVALID_REQUEST,
          message: failureMessage(cause),
        }),
    });
  });

/** A reply the gateway sent that its schema rejects ends the call as a broken contract. */
const checkOutput = (method: string, bytes: Uint8Array): void => {
  try {
    validateServicePayload(SERVICES, method, 'output', bytes);
  } catch (cause) {
    throw new GrpcFailed({
      code: GrpcCode.DATA_LOSS,
      message: `invalid ${method.split('/').at(-1)} response: ${String(cause)}`,
    });
  }
};

/** The host's connection, kept up for as long as `use` runs. */
type Using = <A>(
  host: HostKey,
  method: string,
  use: (connection: GatewayConnection) => Effect.Effect<A, CallError>,
) => Effect.Effect<A, CallError>;

/** The one log line for a call that failed without its caller cancelling it. */
const failureLine = (method: string, error: CallError): string => {
  const code = grpcCodeOf(error);
  return `gateway call failed: ${method} (${code === undefined ? 'no status' : `code ${code}`}): ${error.message.slice(0, 512)}`;
};

const unaryCall =
  (using: Using): GatewayCalls['unary'] =>
  (host, method, bytes) =>
    checkInput(method, bytes).pipe(
      Effect.andThen(() =>
        using(host, method, connection =>
          connection.unary(method, bytes).pipe(
            Effect.tap(reply =>
              Effect.try({
                try: () => checkOutput(method, reply),
                catch: cause => cause as GrpcFailed,
              }),
            ),
          ),
        ),
      ),
    );

const streamCall =
  (using: Using): GatewayCalls['stream'] =>
  (host, method, bytes, receive) =>
    checkInput(method, bytes).pipe(
      Effect.andThen(() =>
        using(host, method, connection =>
          connection.stream(method, bytes, reply => {
            checkOutput(method, reply);
            receive(reply);
          }),
        ),
      ),
    );

/**
 * The calls of the window over one connection per host. `metadata` is what every call of a new
 * connection carries (who is calling, `state/viewer.ts`), asked for when the connection is made.
 */
export const makeGatewayCalls = (
  dialerFor: (host: HostKey) => GatewayDialer,
  log: (host: HostKey, line: string) => void,
  metadata: Effect.Effect<CallMetadata> = Effect.succeed({}),
): Effect.Effect<GatewayCalls, never, Scope.Scope> =>
  Effect.gen(function* () {
    const scope = yield* Scope.Scope;
    const entries = new Map<HostKey, Entry>();
    let closed = false;

    const acquire = (host: HostKey): Effect.Effect<Entry, GatewayFailed> =>
      Effect.gen(function* () {
        if (closed) return yield* new GatewayFailed({message: 'gateway bridge closed'});
        let entry = entries.get(host);
        if (entry !== undefined && (yield* entry.connection.ended)) {
          // An offline connection is gone for good: this call gets a new one.
          const old = entry;
          entries.delete(host);
          yield* Scope.close(old.scope, Exit.void);
          entry = undefined;
        }
        if (entry === undefined) {
          const scope = yield* Scope.make();
          const carried = yield* metadata;
          const connection = yield* makeGatewayConnection(
            dialerFor(host),
            line => log(host, line),
            duplex => openSession(duplex, carried),
          ).pipe(Effect.provideService(Scope.Scope, scope));
          entry = {connection, scope, users: 0, idles: 0};
          entries.set(host, entry);
        }
        entry.users += 1;
        return entry;
      });

    const close = (host: HostKey, entry: Entry): Effect.Effect<void> =>
      Effect.suspend(() => {
        if (entries.get(host) === entry) entries.delete(host);
        return Scope.close(entry.scope, Exit.void);
      });

    /**
     * A connection with no call left stays up for `CONNECTION_LINGER_MS`: a connect is a handshake
     * and then a feed, and the feed finds the connection the handshake dialed. A call within the
     * linger takes it back; one after it dials anew.
     */
    const release = (host: HostKey, entry: Entry): Effect.Effect<void> =>
      Effect.suspend(() => {
        entry.users -= 1;
        if (entry.users > 0) return Effect.void;
        entry.idles += 1;
        const idle = entry.idles;
        return Effect.sleep(`${CONNECTION_LINGER_MS} millis`).pipe(
          Effect.andThen(
            Effect.suspend(() =>
              entry.users === 0 && entry.idles === idle ? close(host, entry) : Effect.void,
            ),
          ),
          Effect.forkIn(scope),
          Effect.asVoid,
        );
      });

    /** `use` on the host's connection, which stays up until it returns. */
    const using = <A>(
      host: HostKey,
      method: string,
      use: (connection: GatewayConnection) => Effect.Effect<A, CallError>,
    ): Effect.Effect<A, CallError> =>
      Effect.acquireUseRelease(
        acquire(host),
        entry => use(entry.connection),
        entry => release(host, entry),
      ).pipe(
        // A call its caller did not cancel and that fails is named once: method and status.
        Effect.tapError(error => Effect.sync(() => log(host, failureLine(method, error)))),
      );

    yield* Effect.addFinalizer(() =>
      Effect.suspend(() => {
        closed = true;
        const open = [...entries.values()];
        entries.clear();
        return Effect.forEach(open, entry => Scope.close(entry.scope, Exit.void), {
          discard: true,
        });
      }),
    );
    return {
      warm: (host, hold) =>
        Effect.acquireUseRelease(
          acquire(host),
          () => Effect.sleep(hold),
          entry => release(host, entry),
        ).pipe(Effect.ignore, Effect.forkIn(scope), Effect.asVoid),
      unary: unaryCall(using),
      stream: streamCall(using),
    } satisfies GatewayCalls;
  });
