import {BackoffSchedule, DEFAULT_RECONNECT_DELAYS_MS} from './backoff.js';
import {BackendClientError} from './errors.js';
import type {ProtocolRequest, ProtocolResponse, RequestInput} from './protocol.js';
import {parseProtocolResponse, responseError, streamFailure} from './protocol-parse.js';
import {
  type AbortSignalLike,
  abortReason,
  type RequestOptions,
  type RequestPolicy,
  resolveRequestPolicy,
} from './request-policy.js';

/**
 * Whether the control channel holds a live connection, mirroring the
 * subscription's `StreamConnectionState` vocabulary so a consumer folds both
 * the same way. `disconnected` carries the error that dropped it.
 */
export type ControlChannelState =
  | {readonly status: 'connected'}
  | {readonly status: 'disconnected'; readonly error: Error};

/**
 * The timer seam every deadline and redial delay goes through. Returns a
 * cancel function rather than a handle, so the type names no runtime's timer
 * object and a test can drive deadlines without waiting for them.
 */
type ScheduleTimeout = (callback: () => void, delayMs: number) => () => void;

/**
 * The real-timer implementation of the seam, shared so a transport and the
 * channel it drives do not each carry a copy of the same four lines.
 */
const defaultScheduleTimeout: ScheduleTimeout = (callback, delayMs) => {
  const timer = setTimeout(callback, delayMs);
  return () => clearTimeout(timer);
};

/**
 * Where one live connection routes what it receives. Bound per dial, so a
 * superseded connection's late event is attributed to the connection it came
 * from and discarded rather than mistaken for the live one's.
 */
export interface ControlConnectionHandlers {
  /** One complete protocol frame, already delimited by the transport. */
  onFrame(frame: string): void;
  /**
   * The connection failed as a transport: it closed, errored, or refused a
   * write. A redial may recover it, so the channel backs off and retries.
   */
  onDrop(error: Error): void;
  /**
   * The peer sent bytes this protocol cannot read. Not a transient outage a
   * redial recovers, so the channel fails what it owes instead of resending.
   */
  onFault(error: Error): void;
}

/**
 * A request whose envelope is already filled in, so its id is known to be
 * present. The protocol's generated request types leave `request_id` optional
 * (the server stamps one when a client omits it); every request this channel
 * issues carries one, and the correlation paths rely on it.
 */
export type IssuedRequest = ProtocolRequest & {readonly request_id: string};

/** One live control connection, owned by the channel that opened it. */
export interface ControlConnection {
  /**
   * Write one frame. A write failure is reported through `onDrop`, never by
   * throwing, so the channel's disposition path owns every delivery failure.
   */
  send(frame: string): void;
  /** Release the connection; resolves once it is actually closed. */
  close(): Promise<void>;
}

/**
 * How a transport makes and un-makes connections. This is the whole seam
 * between the control-channel state machine and the bytes: the channel decides
 * what happens to a request, and the connector decides how a connection is
 * dialed, framed, and torn down.
 */
interface ControlConnector {
  /**
   * Dial one control connection, or reject with a typed dial failure. The
   * handlers are bound before the connection is used, so no frame is lost.
   */
  open(handlers: ControlConnectionHandlers): Promise<ControlConnection>;
  /**
   * Run one request on its own connection, with no response deadline, and
   * settle when it answers, fails, or the signal aborts it. Which requests
   * come here is the channel's decision (`RequestPolicy.dedicatedConnection`);
   * how a one-shot connection is opened and closed is the transport's.
   *
   * The channel has already rejected a signal that was aborted before this
   * call, so an implementation only owes the listener. One that awaits anything
   * before attaching that listener must re-check `signal.aborted` afterwards:
   * an abort in that window never fires the event, and the request would
   * otherwise run on a connection the caller has given up on.
   */
  runDedicated(request: IssuedRequest, signal?: AbortSignalLike): Promise<ProtocolResponse>;
}

interface ControlChannelOptions {
  /** Stable frontend identity stamped on every request envelope. */
  readonly clientId: string;
  /** Response deadline for a request with no table entry or per-call override. */
  readonly requestTimeoutMs?: number | undefined;
  /**
   * Backoff schedule for redials after a drop; its length bounds the attempts
   * per outage. Defaults to the schedule the subscription shares.
   */
  readonly reconnectDelaysMs?: readonly number[] | undefined;
  /** Observe connectivity changes; see `ControlChannelState`. */
  readonly onConnectionState?: ((state: ControlChannelState) => void) | undefined;
  /** Timer seam; defaults to real timers. */
  readonly scheduleTimeout?: ScheduleTimeout | undefined;
}

const DEFAULT_REQUEST_TIMEOUT_MS = 30_000;

/**
 * One control request the channel owes an answer for. It carries everything
 * the send, disconnect, resend, and cancel paths need, so a request in flight
 * at a drop can be disposed by its policy without reconstructing any of it.
 * `requestId` is replaced on a resend so an id is never reused; `cancelTimeout`
 * is set only while the request is on the wire.
 */
interface ControlRequest {
  request: IssuedRequest;
  /** The `#pending` key: a fresh, never-reused value on every (re)send. */
  requestId: string;
  readonly policy: RequestPolicy;
  readonly resolve: (value: ProtocolResponse) => void;
  readonly reject: (error: Error) => void;
  readonly timeoutMs: number;
  detachAbort: () => void;
  cancelTimeout: (() => void) | null;
}

/**
 * The channel's connectivity, with the live connection carried by the one state
 * that has one. Modelling it as a union rather than a status plus a nullable
 * socket makes "connected implies a connection to write to" a type, so no send
 * path has an unreachable branch to guess at.
 *
 * - `connected`: requests go straight onto the connection.
 * - `reconnecting`: an outage the backoff loop is working.
 * - `disconnected`: no connection and no redial pending, because the schedule
 *   is spent or a protocol fault took the channel down. `reconnect()` revives
 *   it; until then the disconnect is the channel's answer.
 * - `closed`: terminal.
 */
type ControlPhase =
  | {readonly kind: 'connected'; readonly connection: ControlConnection}
  | {readonly kind: 'reconnecting'}
  | {readonly kind: 'disconnected'}
  | {readonly kind: 'closed'};

/**
 * A failure a connection reported before the dial that owns it had installed
 * it. `fault` separates the two dispositions: unreadable bytes are a real
 * answer about the peer, a close is an outage the schedule may still recover.
 */
interface DialReport {
  readonly error: Error;
  readonly fault: boolean;
}

/**
 * The multiplexed request/response half of a client's connection to the server,
 * independent of how its bytes travel.
 *
 * It owns everything that decides what happens to a request: the envelope and
 * its never-reused id, the per-type policy lookup, the response deadline, the
 * redial loop over the shared backoff schedule, the disposition of requests in
 * flight when a connection drops, cancellation, and the connectivity a frontend
 * reads to disable controls it cannot deliver. A transport supplies only a
 * `ControlConnector`, so every transport answers each of those questions the
 * same way.
 *
 * Guarantees:
 *
 * - Every request settles exactly once: it resolves with the server's response,
 *   rejects with a typed `BackendClientError`, or rejects with its abort reason.
 *   No path leaves a caller waiting on a channel that is not coming back.
 * - A request id is never reused. A resend mints a fresh one, and a response
 *   whose id is not outstanding is discarded, so no late frame from a dead
 *   connection (or from an abandoned request) can resolve a later request.
 * - Only an idempotent request survives an outage. One that must not be
 *   repeated fails typed rather than risking a double effect.
 */
export class ControlChannel {
  readonly #connector: ControlConnector;
  readonly #clientId: string;
  readonly #requestTimeoutMs: number;
  readonly #backoff: BackoffSchedule;
  readonly #onConnectionState: ((state: ControlChannelState) => void) | undefined;
  readonly #scheduleTimeout: ScheduleTimeout;
  /** Requests on the wire, by the id they were written with. */
  readonly #pending = new Map<string, ControlRequest>();
  /**
   * Idempotent requests waiting for the redial that recovers an outage. A
   * request that must not be repeated never waits one out; it fails at the drop.
   */
  readonly #held: ControlRequest[] = [];

  #phase: ControlPhase = {kind: 'disconnected'};
  /**
   * Identifies the live connection. Each dial takes the next value and binds it
   * into that connection's handlers, so an event from a connection the channel
   * has already moved past fails the check and is ignored.
   */
  #generation = 0;
  #cancelRedial: (() => void) | null = null;
  #dialing = false;
  /**
   * A failure the connection a dial is still resolving has already reported.
   * `ControlConnector.open` binds the handlers before it returns the connection,
   * so a transport can report a close or a fault for a connection the channel
   * has not installed yet. The phase guards on `#onDrop` and `#onFault` cannot
   * act on that (there is no live connection to dispose), so the report is
   * recorded here and the dial fails the attempt instead of installing a
   * connection it has already been told is dead. Cleared at the start of every
   * dial, so one attempt's report cannot condemn the next one.
   */
  #reportedWhileDialing: DialReport | null = null;
  /**
   * The connectivity the caller has been told about, so one outage that fails
   * several redials in a row reports `disconnected` once rather than per
   * attempt, and a recovery reports `connected` once.
   */
  #reported: 'connected' | 'disconnected' | undefined;

  constructor(connector: ControlConnector, options: ControlChannelOptions) {
    this.#connector = connector;
    this.#clientId = options.clientId;
    this.#requestTimeoutMs = options.requestTimeoutMs ?? DEFAULT_REQUEST_TIMEOUT_MS;
    this.#backoff = new BackoffSchedule(options.reconnectDelaysMs ?? DEFAULT_RECONNECT_DELAYS_MS);
    this.#onConnectionState = options.onConnectionState;
    this.#scheduleTimeout = options.scheduleTimeout ?? defaultScheduleTimeout;
  }

  /**
   * Take over a connection the caller already dialed, as the channel's live
   * one. A transport whose constructor is handed an open connection (the Node
   * client, which dials before it can report success) calls this once, before
   * any request. The adopted connection is the caller's starting point, so it is
   * not reported as a change.
   */
  adopt(connect: (handlers: ControlConnectionHandlers) => ControlConnection): void {
    this.#phase = {kind: 'connected', connection: connect(this.#bindHandlers())};
    this.#reported ??= 'connected';
  }

  /**
   * Send a control request and resolve with the server's response.
   *
   * The per-request `options` are the one seam for per-type policy: the request
   * type's table entry (`request-policy.ts`) sets whether it is idempotent,
   * runs on its own connection, and its deadline; `options` override the
   * connection and the deadline for one call and carry an `AbortSignal`.
   */
  request(input: RequestInput, options: RequestOptions = {}): Promise<ProtocolResponse> {
    if (this.#phase.kind === 'closed') {
      return Promise.reject(disconnectedError('Client is closed'));
    }
    const signal = options.signal;
    if (signal?.aborted) return Promise.reject(abortReason(signal));
    const policy = resolveRequestPolicy(input.type, options);
    const requestId = newRequestId();
    const request = this.#envelope(input, requestId);
    if (policy.dedicatedConnection) return this.#connector.runDedicated(request, signal);
    return new Promise((resolve, reject) => {
      const entry: ControlRequest = {
        request,
        requestId,
        policy,
        resolve,
        reject,
        timeoutMs: policy.timeoutMs ?? this.#requestTimeoutMs,
        detachAbort: () => {},
        cancelTimeout: null,
      };
      if (signal !== undefined) {
        const onAbort = (): void => this.#abort(entry, abortReason(signal));
        signal.addEventListener('abort', onAbort, {once: true});
        entry.detachAbort = () => signal.removeEventListener('abort', onAbort);
      }
      this.#enqueue(entry);
    });
  }

  /**
   * Redial now, outside the backoff schedule. Once the schedule is spent, or a
   * protocol fault has taken the channel down, it stays down with the
   * disconnect as its answer; this is the entry a caller uses to try again (a
   * reconnect affordance, or a resume-after-sleep watcher such as #832). It
   * no-ops unless the channel is in that spent state, so it cannot stack a
   * second dial onto a healthy or already-retrying one.
   */
  reconnect(): void {
    if (this.#phase.kind !== 'disconnected') return;
    this.#phase = {kind: 'reconnecting'};
    this.#backoff.reset();
    this.#scheduleRedial();
  }

  /**
   * Whether the channel currently holds a live connection. False while a redial
   * is outstanding and once the schedule is spent, so a frontend can disable
   * the controls a dropped channel cannot carry.
   * `ControlChannelOptions.onConnectionState` reports the same transitions as
   * they happen.
   */
  get connected(): boolean {
    return this.#phase.kind === 'connected';
  }

  /**
   * Close the channel: cancel a pending redial, fail every request still owed
   * an answer (on the wire or held for a resend), and release the live
   * connection. Terminal; a later request rejects rather than dialing.
   */
  async close(): Promise<void> {
    const phase = this.#phase;
    this.#phase = {kind: 'closed'};
    this.#cancelRedial?.();
    this.#cancelRedial = null;
    this.#failOwed(disconnectedError('Client closed'));
    if (phase.kind === 'connected') await phase.connection.close();
  }

  /** Route one request by the channel's state: send it, hold it, or fail it. */
  #enqueue(entry: ControlRequest): void {
    const phase = this.#phase;
    if (phase.kind === 'connected') {
      this.#send(entry, phase.connection);
      return;
    }
    // An outage the backoff loop is working: hold an idempotent request to
    // resend once the channel recovers; fail one that must not be repeated so
    // the caller, seeing the disconnected state, decides whether to reissue it.
    // Anything else (a spent schedule, a fault, a closed channel) has no
    // recovery pending, so the disconnect is the answer for every type.
    if (phase.kind === 'reconnecting' && entry.policy.idempotent) {
      this.#held.push(entry);
      return;
    }
    this.#settleReject(
      entry,
      phase.kind === 'closed' ? disconnectedError('Client is closed') : disconnectedError(),
    );
  }

  /** Write one request to the live connection and arm its response deadline. */
  #send(entry: ControlRequest, connection: ControlConnection): void {
    const requestId = entry.requestId;
    entry.cancelTimeout = this.#scheduleTimeout(() => {
      entry.cancelTimeout = null;
      this.#pending.delete(requestId);
      this.#settleReject(
        entry,
        new BackendClientError('timeout', `Server request timed out after ${entry.timeoutMs}ms`),
      );
    }, entry.timeoutMs);
    this.#pending.set(requestId, entry);
    connection.send(JSON.stringify(entry.request));
  }

  #envelope(input: RequestInput, requestId: string): IssuedRequest {
    return {
      protocol_version: 1,
      request_id: requestId,
      client_id: this.#clientId,
      timestamp: new Date().toISOString(),
      ...input,
    } as IssuedRequest;
  }

  /**
   * Bind a fresh generation into one connection's handlers. The channel reads
   * the generation rather than comparing connection objects, so a handler that
   * fires while its `open()` is still resolving is attributed correctly too.
   */
  #bindHandlers(): ControlConnectionHandlers {
    this.#generation += 1;
    const generation = this.#generation;
    const live = (): boolean => generation === this.#generation;
    return {
      onFrame: frame => {
        if (live()) this.#onFrame(frame);
      },
      onDrop: error => {
        if (live()) this.#onDrop(error);
      },
      onFault: error => {
        if (live()) this.#onFault(error);
      },
    };
  }

  #onFrame(frame: string): void {
    let response: ProtocolResponse;
    try {
      response = parseProtocolResponse(frame);
    } catch (error) {
      this.#onFault(streamFailure(error));
      return;
    }
    const entry = this.#pending.get(response.request_id);
    // No match means a retired, cancelled, or otherwise unknown id: discard it
    // rather than misroute it onto a request that did not issue it.
    if (entry === undefined) return;
    this.#pending.delete(response.request_id);
    if (response.ok) this.#settleResolve(entry, response);
    else this.#settleReject(entry, responseError(response));
  }

  /**
   * A live connection dropped. Transition once (a stale connection's late event
   * or a shutdown is ignored), report the disconnect, dispose what was in
   * flight by policy, and start the backoff loop.
   */
  #onDrop(error: Error): void {
    const phase = this.#phase;
    if (phase.kind !== 'connected') {
      this.#recordWhileDialing(error, false);
      return;
    }
    this.#phase = {kind: 'reconnecting'};
    this.#report({status: 'disconnected', error});
    void phase.connection.close();
    this.#disposeForOutage();
    this.#backoff.reset();
    this.#scheduleRedial();
  }

  /**
   * A protocol fault on the live connection: the peer sent bytes this client
   * cannot read (malformed JSON, an unknown message, an unsupported version, an
   * unframable stream). Unlike a drop this is not a transient outage a redial
   * recovers, and it is a real answer about every request in flight, so each
   * fails with the typed error rather than being silently resent. The channel
   * settles into the dead state that `reconnect()` can revive if a caller
   * decides the fault was one-off.
   */
  #onFault(error: Error): void {
    const phase = this.#phase;
    if (phase.kind !== 'connected') {
      this.#recordWhileDialing(error, true);
      return;
    }
    this.#phase = {kind: 'disconnected'};
    this.#report({status: 'disconnected', error});
    void phase.connection.close();
    this.#failOwed(error);
  }

  /**
   * Keep the first failure a dial's own connection reports before that dial has
   * installed it. Only the first: it is the one that explains the connection,
   * and a broken transport may report several. Outside a dial there is nothing
   * to condemn, so the report is the stale event the phase guard took it for.
   */
  #recordWhileDialing(error: Error, fault: boolean): void {
    if (!this.#dialing) return;
    this.#reportedWhileDialing ??= {error, fault};
  }

  /**
   * Take whatever the dialing connection reported and leave the slot empty, so
   * one attempt's report cannot be read twice or condemn the next attempt.
   */
  #takeDialReport(): DialReport | null {
    const reported = this.#reportedWhileDialing;
    this.#reportedWhileDialing = null;
    return reported;
  }

  /**
   * One redial attempt. Never rejects: a failure is an outage the backoff loop
   * owns, and a connection nobody wants any more is closed rather than kept.
   */
  async #dial(): Promise<void> {
    if (this.#phase.kind !== 'reconnecting' || this.#dialing) return;
    this.#dialing = true;
    // Discard an earlier attempt's report before this one can be blamed for it.
    this.#takeDialReport();
    try {
      let connection: ControlConnection;
      const handlers = this.#bindHandlers();
      try {
        connection = await this.#connector.open(handlers);
      } catch {
        // This attempt failed; back off again, or exhaust the schedule.
        if (this.#phase.kind === 'reconnecting') this.#scheduleRedial();
        return;
      }
      if (this.#phase.kind !== 'reconnecting') {
        // close(), or a revive, raced the dial: this connection is not wanted.
        void connection.close();
        return;
      }
      const reported = this.#takeDialReport();
      if (reported !== null) {
        // The connection failed before this dial could install it. Installing it
        // anyway would make `connected` true for a connection that will never
        // answer and never report again (a socket emits `close` once), leaving
        // every later request to wait out its response deadline. So the attempt
        // takes the failure it was handed: a drop is an outage the schedule may
        // still recover, a fault is not.
        void connection.close();
        if (reported.fault) {
          this.#phase = {kind: 'disconnected'};
          this.#report({status: 'disconnected', error: reported.error});
          this.#failOwed(reported.error);
        } else {
          this.#report({status: 'disconnected', error: reported.error});
          this.#scheduleRedial();
        }
        return;
      }
      this.#phase = {kind: 'connected', connection};
      this.#backoff.reset();
      this.#report({status: 'connected'});
      this.#flushHeld(connection);
    } finally {
      this.#dialing = false;
    }
  }

  /**
   * Dispose everything owed across an outage: an idempotent request rides the
   * recovery, one that must not be repeated fails now with a typed disconnect.
   * One rule for both the requests that were on the wire and the ones already
   * waiting for a connection.
   */
  #disposeForOutage(): void {
    const owed = [...this.#pending.values(), ...this.#held.splice(0)];
    this.#pending.clear();
    for (const entry of owed) {
      this.#cancelDeadline(entry);
      if (entry.policy.idempotent) this.#held.push(entry);
      else this.#settleReject(entry, disconnectedError());
    }
  }

  #scheduleRedial(): void {
    if (this.#cancelRedial !== null) return;
    const delayMs = this.#backoff.next();
    if (delayMs === undefined) {
      this.#exhaust();
      return;
    }
    this.#cancelRedial = this.#scheduleTimeout(() => {
      this.#cancelRedial = null;
      void this.#dial();
    }, delayMs);
  }

  /**
   * The finite schedule is spent: no connection is coming without a caller
   * asking for one, so fail every held request and settle into the reportable
   * dead state that `reconnect()` revives.
   */
  #exhaust(): void {
    this.#phase = {kind: 'disconnected'};
    for (const entry of this.#held.splice(0)) this.#settleReject(entry, disconnectedError());
  }

  #flushHeld(connection: ControlConnection): void {
    for (const entry of this.#held.splice(0)) {
      // Mint a fresh id: the id this request last carried may have been written
      // to a connection that is now dead, so reusing it could let a late frame
      // from that connection match the resend. A never-reused id closes that
      // off, and costs an unused id for a request that was never written.
      const nextId = newRequestId();
      entry.request = {...entry.request, request_id: nextId} as IssuedRequest;
      entry.requestId = nextId;
      this.#send(entry, connection);
    }
  }

  /** Free an abandoned request's slot and reject it; its id is never reused. */
  #abort(entry: ControlRequest, error: Error): void {
    if (this.#pending.get(entry.requestId) === entry) this.#pending.delete(entry.requestId);
    const heldIndex = this.#held.indexOf(entry);
    if (heldIndex !== -1) this.#held.splice(heldIndex, 1);
    this.#settleReject(entry, error);
  }

  #failOwed(error: Error): void {
    const owed = [...this.#pending.values(), ...this.#held.splice(0)];
    this.#pending.clear();
    for (const entry of owed) this.#settleReject(entry, error);
  }

  #cancelDeadline(entry: ControlRequest): void {
    entry.cancelTimeout?.();
    entry.cancelTimeout = null;
  }

  #settleReject(entry: ControlRequest, error: Error): void {
    this.#cancelDeadline(entry);
    entry.detachAbort();
    entry.reject(error);
  }

  #settleResolve(entry: ControlRequest, response: ProtocolResponse): void {
    this.#cancelDeadline(entry);
    entry.detachAbort();
    entry.resolve(response);
  }

  #report(state: ControlChannelState): void {
    if (this.#reported === state.status) return;
    this.#reported = state.status;
    this.#onConnectionState?.(state);
  }
}

function newRequestId(): string {
  return globalThis.crypto.randomUUID();
}

/**
 * A typed disconnect for a request the channel cannot carry. The message is the
 * same whatever ended the channel, so callers branch on `kind` rather than on
 * text, and `ControlChannelState.disconnected` carries the failure that
 * actually ended it for a frontend to show.
 */
function disconnectedError(message = 'Server is disconnected'): BackendClientError {
  return new BackendClientError('disconnected', message);
}
