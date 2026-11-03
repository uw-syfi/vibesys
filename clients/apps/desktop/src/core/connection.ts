/**
 * The link to one host. It owns the only retry loop: a feed that sees a retryable error calls
 * `suspect` and waits for `ready` again, so the wait and the count live in one place. Ready means
 * the orchestrator's Hello was answered COMPATIBLE, never merely that a socket is open.
 */
import {Compatibility} from '@vibesys/orchestrator-proto';
import * as Clock from 'effect/Clock';
import * as Context from 'effect/Context';
import * as Data from 'effect/Data';
import * as Deferred from 'effect/Deferred';
import * as Duration from 'effect/Duration';
import * as Effect from 'effect/Effect';
import * as Option from 'effect/Option';
import * as Queue from 'effect/Queue';
import * as Ref from 'effect/Ref';
import * as Atom from 'effect/reactivity/Atom';
import * as Stream from 'effect/Stream';
import * as SubscriptionRef from 'effect/SubscriptionRef';
import {type Fault, type HostConnectionState, idleConnection, isTerminal} from '../state/index.js';
import {type AppError, InvalidMessage, isRetryable} from './errors.js';
import {faultOf} from './fault.js';
import type {OrchestratorClient} from './orchestrator-client.js';
import {CONNECT_DEADLINE} from './policy.js';
import {CLIENT_BUILD} from './protocol.js';
import {
  defaultReconnect,
  failuresAfter,
  type ReconnectPolicy,
  reconnectDelay,
} from './reconnect.js';

type Terminal = Extract<HostConnectionState, {readonly _tag: 'Incompatible' | 'Unusable'}>;

/** The connection cannot succeed until the user retries; `state` says why. */
export class ConnectionTerminal extends Data.TaggedError('ConnectionTerminal')<{
  readonly state: Terminal;
}> {}

export class HostConnection extends Context.Service<
  HostConnection,
  {
    readonly state: Atom.Atom<HostConnectionState>;
    /**
     * Suspends until the connection is Ready at an epoch greater than `after` (any, by default),
     * then answers that epoch. Fails when the connection is terminal. The first call dials.
     */
    readonly ready: (after?: number) => Effect.Effect<number, ConnectionTerminal>;
    /** A feed saw `error` on `epoch`. A no-op when that epoch is already over. */
    readonly suspect: (epoch: number, error: AppError) => Effect.Effect<void>;
    /** The user asked to try again: skips a wait, or leaves a terminal state. */
    readonly retry: Effect.Effect<void>;
    /** Suspends while the connection is terminal; returns once the user's retry leaves that state. */
    readonly leftTerminal: Effect.Effect<void>;
  }
>()('app/HostConnection') {}

/** What a feed holds of a `HostConnection`. */
export type HostConnectionApi = HostConnection['Service'];

interface OpenEpoch {
  readonly epoch: number;
  readonly lost: Deferred.Deferred<AppError>;
}

/** What the dial loop and the service share. */
interface Wiring {
  readonly client: OrchestratorClient;
  readonly policy: ReconnectPolicy;
  readonly state: SubscriptionRef.SubscriptionRef<HostConnectionState>;
  /** The Ready epoch that is open, and the signal that ends it. */
  readonly current: Ref.Ref<OpenEpoch | null>;
  /** User retries, at most one pending. */
  readonly retries: Queue.Queue<true>;
}

/** Wait out the failure, or stop for good if retrying cannot help. Answers the next failure count. */
const afterFailure = (w: Wiring, error: AppError, failures: number) =>
  Effect.gen(function* () {
    const fault: Fault = faultOf(error);
    if (!isRetryable(error)) {
      yield* SubscriptionRef.set(w.state, {_tag: 'Unusable', fault});
      yield* Queue.take(w.retries);
      return 0;
    }
    const delay = yield* reconnectDelay(w.policy, failures);
    const now = yield* Clock.currentTimeMillis;
    yield* SubscriptionRef.set(w.state, {
      _tag: 'Waiting',
      attempt: failures,
      fault,
      retryAtMs: now + Duration.toMillis(delay),
    });
    return yield* Effect.raceFirst(
      Effect.as(Effect.sleep(delay), failures),
      Effect.as(Queue.take(w.retries), 0),
    );
  });

const failedHello = (w: Wiring, error: AppError, failures: number) =>
  afterFailure(w, error, failuresAfter(w.policy, failures, null));

/** One pass of the loop: dial, then stay until the connection is lost. Answers the next count. */
const connectOnce = (w: Wiring, failures: number, epoch: number) =>
  Effect.gen(function* () {
    yield* Queue.clear(w.retries);
    yield* SubscriptionRef.set(w.state, {_tag: 'Connecting', attempt: failures});
    const hello = yield* w.client
      .hello({client: CLIENT_BUILD}, {deadline: CONNECT_DEADLINE})
      .pipe(Effect.result);
    if (hello._tag === 'Failure') {
      return {failures: yield* failedHello(w, hello.failure, failures), epoch};
    }
    const {daemon, compatibility} = hello.success;
    if (compatibility !== Compatibility.COMPATIBLE) {
      const side =
        compatibility === Compatibility.CLIENT_TOO_OLD ? 'client-too-old' : 'daemon-too-old';
      yield* SubscriptionRef.set(w.state, {_tag: 'Incompatible', side, daemon});
      yield* Queue.take(w.retries);
      return {failures: 0, epoch};
    }
    if (daemon === undefined) {
      const missing = new InvalidMessage({
        direction: 'response',
        type: 'HelloResponse',
        detail: 'daemon is missing',
      });
      return {failures: yield* failedHello(w, missing, failures), epoch};
    }
    const next = epoch + 1;
    const lost = yield* Deferred.make<AppError>();
    yield* Ref.set(w.current, {epoch: next, lost});
    const readyAt = yield* Clock.currentTimeMillis;
    yield* SubscriptionRef.set(w.state, {_tag: 'Ready', epoch: next, daemon});
    const error = yield* Deferred.await(lost);
    yield* Ref.set(w.current, null);
    const readyFor = (yield* Clock.currentTimeMillis) - readyAt;
    const count = yield* afterFailure(w, error, failuresAfter(w.policy, failures, readyFor));
    return {failures: count, epoch: next};
  });

const dialForever = (w: Wiring) =>
  Effect.gen(function* () {
    let pass = {failures: 0, epoch: 0};
    while (true) pass = yield* connectOnce(w, pass.failures, pass.epoch);
  });

/** A connection to one host through `client`, living as long as the surrounding `Scope`. */
export const makeHostConnection = Effect.fn('HostConnection.make')(function* (
  client: OrchestratorClient,
  policy: ReconnectPolicy = defaultReconnect,
) {
  const scope = yield* Effect.scope;
  const w: Wiring = {
    client,
    policy,
    state: yield* SubscriptionRef.make<HostConnectionState>(idleConnection),
    current: yield* Ref.make<OpenEpoch | null>(null),
    retries: yield* Queue.sliding<true>(1),
  };
  const started = yield* Ref.make(false);

  const start = Effect.gen(function* () {
    if (!(yield* Ref.getAndSet(started, true))) yield* Effect.forkIn(dialForever(w), scope);
  });

  const ready = Effect.fn('HostConnection.ready')(function* (after = -1) {
    yield* start;
    const settled = yield* SubscriptionRef.changes(w.state).pipe(
      Stream.filter(s => (s._tag === 'Ready' && s.epoch > after) || isTerminal(s)),
      Stream.runHead,
    );
    const s = Option.getOrThrow(settled);
    if (s._tag === 'Ready') return s.epoch;
    return yield* new ConnectionTerminal({state: s as Terminal});
  });

  const suspect = Effect.fn('HostConnection.suspect')(function* (epoch: number, error: AppError) {
    const held = yield* Ref.get(w.current);
    if (held !== null && held.epoch === epoch) yield* Deferred.succeed(held.lost, error);
  });

  const retry = Effect.gen(function* () {
    const s = yield* SubscriptionRef.get(w.state);
    if (s._tag === 'Waiting' || isTerminal(s)) yield* Queue.offer(w.retries, true);
  });

  const leftTerminal = SubscriptionRef.changes(w.state).pipe(
    Stream.filter(s => !isTerminal(s)),
    Stream.runHead,
    Effect.asVoid,
  );

  return HostConnection.of({
    state: Atom.keepAlive(Atom.subscriptionRef(w.state)),
    ready,
    suspect,
    retry,
    leftTerminal,
  });
});
