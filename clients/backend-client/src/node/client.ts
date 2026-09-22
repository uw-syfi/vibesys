import {createConnection, type Socket} from 'node:net';
import {BackoffSchedule, DEFAULT_RECONNECT_DELAYS_MS} from '../backoff.js';
import {BackendClientError, ServerError} from '../errors.js';
import {NewlineFramer} from '../newline-framer.js';
import type {
  Diagnostic,
  ProtocolRequest,
  ProtocolResponse,
  RequestInput,
  ServerMessage,
} from '../protocol.js';
import {
  type AbortSignalLike,
  type RequestOptions,
  type RequestPolicy,
  resolveRequestPolicy,
} from '../request-policy.js';
import type {EventSubscription, SubscribeOptions} from '../transport.js';

/**
 * Whether the control channel holds a live connection, mirroring the
 * subscription's `StreamConnectionState` vocabulary so a consumer folds both
 * the same way. `disconnected` carries the error that dropped it.
 */
export type ControlChannelState =
  | {readonly status: 'connected'}
  | {readonly status: 'disconnected'; readonly error: Error};

export interface ServerClientOptions {
  connectTimeoutMs?: number;
  requestTimeoutMs?: number;
  /** Delay between connection attempts while the socket does not exist yet. */
  connectRetryIntervalMs?: number;
  /**
   * How long `close()` waits for a socket to end gracefully before destroying
   * it. Bounds shutdown against a server that never sends its FIN, so a quit
   * cannot hang. Applies to every socket the client owns: control, chat, and
   * subscriptions.
   */
  closeGraceMs?: number;
  /**
   * Backoff schedule for control-channel redials after a drop; its length
   * bounds the attempts per outage. Defaults to the schedule the subscription
   * shares; injected by tests so a retry is immediate instead of half a second
   * away.
   */
  reconnectDelaysMs?: readonly number[];
  /**
   * Observe control-channel connectivity: `disconnected` once when a live
   * channel drops, `connected` when a redial recovers it. Not called on the
   * first successful connect (the channel is connected by default). Lets a
   * frontend disable the controls a dropped channel cannot carry.
   */
  onConnectionState?: (state: ControlChannelState) => void;
}

const DEFAULT_CONNECT_TIMEOUT_MS = 5_000;
const DEFAULT_REQUEST_TIMEOUT_MS = 30_000;
const DEFAULT_CONNECT_RETRY_INTERVAL_MS = 25;
/**
 * A graceful end on a local socket completes in well under this; the window is
 * only reached when the peer never sends its FIN, so it stays short to keep
 * `close()` inside the launcher's 2s backend exit grace.
 */
const DEFAULT_CLOSE_GRACE_MS = 250;
/** Errors a not-yet-listening server produces; anything else is fatal. */
const RETRYABLE_CONNECT_CODES = new Set(['ENOENT', 'ECONNREFUSED']);

/**
 * One control request the client owes an answer for. It carries everything the
 * send, disconnect, resend, and cancel paths need, so a request outstanding at
 * a drop can be disposed by its policy (resent if idempotent, failed if not)
 * without reconstructing any of it. `request.request_id` is replaced on a
 * resend so an id is never reused; `timeout` is live only while the request is
 * on the wire.
 */
interface ControlRequest {
  request: ProtocolRequest;
  /** The `#pending` key: a fresh, never-reused value on every (re)send. */
  requestId: string;
  readonly policy: RequestPolicy;
  readonly resolve: (value: ProtocolResponse) => void;
  readonly reject: (error: Error) => void;
  readonly timeoutMs: number;
  detachAbort: () => void;
  timeout: ReturnType<typeof setTimeout> | null;
}

export class ServerClient {
  #socket: Socket;
  readonly #path: string;
  readonly #pending = new Map<string, ControlRequest>();
  /**
   * Idempotent requests waiting for the channel to recover, to resend once it
   * does. A request that must not be repeated never lands here: it fails at the
   * drop instead.
   */
  readonly #held: ControlRequest[] = [];
  readonly #connectTimeoutMs: number;
  readonly #requestTimeoutMs: number;
  readonly #closeGraceMs: number;
  /**
   * Every secondary socket the client has opened (subscriptions and chats),
   * mapped to a hook that suppresses its own disconnect handling. `close()`
   * calls the hook before destroying, so tearing a socket down as part of a
   * client-wide close does not surface as a spurious stream disconnect, whatever
   * order the caller closes the stream and the client in.
   */
  readonly #secondarySockets = new Map<Socket, () => void>();
  readonly #backoff: BackoffSchedule;
  readonly #onConnectionState: ((state: ControlChannelState) => void) | undefined;
  /**
   * The control channel's connectivity. `connected` accepts requests on the
   * live socket; `reconnecting` is an outage the backoff loop is working;
   * `disconnected` is a spent schedule that `reconnect()` can revive; `closed`
   * is terminal. Every send and disconnect path reads it rather than the
   * socket's `destroyed` flag, which cannot tell an outage from a shutdown.
   */
  #controlState: 'connected' | 'reconnecting' | 'disconnected' | 'closed' = 'connected';
  #redialTimer: ReturnType<typeof setTimeout> | null = null;
  #redialing = false;
  /**
   * Whether the caller currently sees the channel as disconnected. One outage
   * can fail many redials in a row; this reports `disconnected` once, on the
   * transition, and `connected` when a redial recovers.
   */
  #disconnectedReported = false;

  private constructor(socket: Socket, path: string, options: ServerClientOptions) {
    this.#socket = socket;
    this.#path = path;
    this.#connectTimeoutMs = options.connectTimeoutMs ?? DEFAULT_CONNECT_TIMEOUT_MS;
    this.#requestTimeoutMs = options.requestTimeoutMs ?? DEFAULT_REQUEST_TIMEOUT_MS;
    this.#closeGraceMs = options.closeGraceMs ?? DEFAULT_CLOSE_GRACE_MS;
    this.#backoff = new BackoffSchedule(options.reconnectDelaysMs ?? DEFAULT_RECONNECT_DELAYS_MS);
    this.#onConnectionState = options.onConnectionState;
    this.#attachControlSocket(socket);
  }

  /**
   * Connect to the server socket, retrying until it accepts.
   *
   * The launcher starts the backend and this client concurrently, so the
   * socket routinely does not exist for the first few hundred milliseconds.
   * Only the errors a starting server produces are retried; every other
   * failure, and the overall deadline, still surfaces to the caller.
   */
  static async connect(path: string, options: ServerClientOptions = {}): Promise<ServerClient> {
    const timeoutMs = options.connectTimeoutMs ?? DEFAULT_CONNECT_TIMEOUT_MS;
    const retryIntervalMs = options.connectRetryIntervalMs ?? DEFAULT_CONNECT_RETRY_INTERVAL_MS;
    const deadline = Date.now() + timeoutMs;
    let lastError: Error | undefined;
    while (true) {
      try {
        return await ServerClient.#connectOnce(path, options, deadline);
      } catch (error) {
        if (!isTransientDialError(error)) throw error;
        lastError = error;
      }
      if (Date.now() + retryIntervalMs >= deadline) {
        throw new BackendClientError(
          'timeout',
          `Timed out connecting to server after ${timeoutMs}ms: ${lastError?.message}`,
          {cause: lastError},
        );
      }
      await delay(retryIntervalMs);
    }
  }

  static #connectOnce(
    path: string,
    options: ServerClientOptions,
    deadline: number,
  ): Promise<ServerClient> {
    const configured = options.connectTimeoutMs ?? DEFAULT_CONNECT_TIMEOUT_MS;
    return dialSocket(
      path,
      Math.max(0, deadline - Date.now()),
      `Timed out connecting to server after ${configured}ms`,
    ).then(socket => new ServerClient(socket, path, options));
  }

  /**
   * Send a control request and resolve with the server's response.
   *
   * The per-request `options` are the one seam for per-type policy: the request
   * type's table entry (`request-policy.ts`) sets whether it is idempotent,
   * runs on its own connection, and its deadline; `options` override the
   * connection and deadline for one call and carry an `AbortSignal`. A chat runs
   * on its own connection with no deadline because it is bounded by its agent,
   * not the control RPC timeout; that is now a table entry, not a special case.
   */
  request(input: RequestInput, options: RequestOptions = {}): Promise<ProtocolResponse> {
    if (this.#controlState === 'closed') {
      return Promise.reject(disconnectedError('Client is closed'));
    }
    const policy = resolveRequestPolicy(input.type, options);
    const requestId = globalThis.crypto.randomUUID();
    const request = {
      protocol_version: 1,
      request_id: requestId,
      timestamp: new Date().toISOString(),
      ...input,
    } as ProtocolRequest;
    if (policy.dedicatedConnection) return this.#requestLongRunning(request, options.signal);
    return new Promise((resolve, reject) => {
      const signal = options.signal;
      if (signal?.aborted) {
        reject(abortReason(signal));
        return;
      }
      const entry: ControlRequest = {
        request,
        requestId,
        policy,
        resolve,
        reject,
        timeoutMs: policy.timeoutMs ?? this.#requestTimeoutMs,
        detachAbort: () => {},
        timeout: null,
      };
      if (signal !== undefined) {
        const onAbort = (): void => this.#abort(entry, abortReason(signal));
        signal.addEventListener('abort', onAbort, {once: true});
        entry.detachAbort = () => signal.removeEventListener('abort', onAbort);
      }
      this.#enqueue(entry);
    });
  }

  subscribe(
    afterSequence: number,
    onMessage: (message: ServerMessage) => void,
    onDisconnect: (error: Error) => void,
    options: SubscribeOptions = {},
  ): Promise<EventSubscription> {
    return new Promise((resolve, reject) => {
      const socket = createConnection(this.#path);
      const frames = new NewlineFramer();
      let subscribed = false;
      let closing = false;
      let disconnected = false;
      let protocolErrorReceived = false;
      const handshakeTimeout = setTimeout(() => {
        disconnect(
          new BackendClientError(
            'timeout',
            `Server subscription timed out after ${this.#connectTimeoutMs}ms`,
          ),
        );
        socket.destroy();
      }, this.#connectTimeoutMs);
      const disconnect = (error: Error): void => {
        if (disconnected || closing) return;
        disconnected = true;
        clearTimeout(handshakeTimeout);
        if (subscribed && protocolErrorReceived) return;
        if (subscribed) onDisconnect(error);
        else reject(error);
      };
      const handleStreamMessage = (message: ServerMessage): boolean => {
        if (message.type === 'protocol_error') {
          protocolErrorReceived = true;
          if (!subscribed) {
            // A structured refusal before the handshake is the server
            // declining the subscription, not the transport failing, so the
            // dial rejects with the refusal rather than with whatever the
            // ensuing close would say.
            disconnect(new ServerError(message.message, message.diagnostic ?? null));
            socket.destroy();
            return false;
          }
        }
        if (!subscribed && message.type === 'subscribed') {
          subscribed = true;
          clearTimeout(handshakeTimeout);
          resolve({
            close: () => {
              closing = true;
              clearTimeout(handshakeTimeout);
              this.#secondarySockets.delete(socket);
              return closeSocketWithin(socket, this.#closeGraceMs);
            },
          });
        }
        return true;
      };
      const handleSubscriptionLine = (line: string): boolean => {
        if (!line) return true;
        try {
          const message = parseServerMessage(line);
          onMessage(message);
          return handleStreamMessage(message);
        } catch (error) {
          disconnect(streamFailure(error));
          socket.destroy();
          return false;
        }
      };
      // Track the socket so close() tears it down, flipping `closing` first.
      this.#secondarySockets.set(socket, () => {
        closing = true;
        clearTimeout(handshakeTimeout);
      });
      socket.setEncoding('utf8');
      socket.once('connect', () => this.#writeSubscribe(socket, afterSequence, options));
      socket.on('data', chunk => {
        const lines = frameChunk(frames, chunk.toString(), error => {
          disconnect(error);
          socket.destroy();
        });
        if (lines === null) return;
        for (const line of lines) {
          if (!handleSubscriptionLine(line)) return;
        }
      });
      socket.once('error', error => disconnect(transportFailure(error)));
      socket.once('close', () => {
        this.#secondarySockets.delete(socket);
        disconnect(
          new BackendClientError(
            'disconnected',
            subscribed
              ? 'Server event stream disconnected'
              : 'Server event stream disconnected before subscription',
          ),
        );
      });
    });
  }

  /** Send the subscribe request once the subscription socket connects. */
  #writeSubscribe(socket: Socket, afterSequence: number, options: SubscribeOptions): void {
    socket.write(
      `${JSON.stringify({
        protocol_version: 1,
        request_id: globalThis.crypto.randomUUID(),
        timestamp: new Date().toISOString(),
        type: 'subscribe',
        after_sequence: afterSequence,
        // Omitted rather than sent as null: an old server forbids unknown fields,
        // so a default subscribe must stay byte-for-byte what it has always been.
        ...(options.tail === undefined ? {} : {tail: options.tail}),
        ...(options.storeId ? {store_id: options.storeId} : {}),
      })}\n`,
    );
  }

  /**
   * Redial now, outside the backoff schedule. Once the schedule is spent the
   * channel stays down with the disconnect as its answer; this is the entry a
   * caller uses to try again: a reconnect affordance, or a resume-after-sleep
   * watcher such as #832. It no-ops unless the channel is in that spent state,
   * so it cannot stack a second dial onto a healthy or already-retrying one.
   */
  reconnect(): void {
    if (this.#controlState !== 'disconnected') return;
    this.#controlState = 'reconnecting';
    this.#backoff.reset();
    this.#scheduleRedial();
  }

  /**
   * Whether the control channel currently holds a live connection. False while
   * reconnecting after an outage, and once the schedule is spent, so a frontend
   * can disable the controls a dropped channel cannot carry. `onConnectionState`
   * reports the same transitions as they happen.
   */
  get connected(): boolean {
    return this.#controlState === 'connected';
  }

  /**
   * Close every socket the client owns: the control connection and every live
   * secondary (subscription or chat). Cancels a pending redial, fails every
   * request still owed an answer (outstanding or held for resend), and ends each
   * socket gracefully, destroying it if it does not close within the grace
   * window, so an unresponsive server cannot hang shutdown.
   */
  close(graceMs: number = this.#closeGraceMs): Promise<void> {
    this.#controlState = 'closed';
    if (this.#redialTimer !== null) {
      clearTimeout(this.#redialTimer);
      this.#redialTimer = null;
    }
    const owed = [...this.#pending.values(), ...this.#held.splice(0)];
    this.#pending.clear();
    for (const entry of owed) this.#settleReject(entry, disconnectedError('Client closed'));
    const secondaries = [...this.#secondarySockets];
    this.#secondarySockets.clear();
    for (const [, suppress] of secondaries) suppress();
    return Promise.all([
      ...secondaries.map(([socket]) => closeSocketWithin(socket, graceMs)),
      closeSocketWithin(this.#socket, graceMs),
    ]).then(() => undefined);
  }

  /**
   * Run an agent-backed request on its own connection without a response timer.
   *
   * Chat duration is bounded by the configured agent, not by the control RPC
   * timeout. A dedicated connection also prevents a long chat from blocking
   * pause, resume, and snapshot requests in the server's per-connection loop. An
   * `AbortSignal` cancels it: the socket is torn down and the promise rejects
   * with the abort reason.
   */
  #requestLongRunning(
    request: ProtocolRequest,
    signal?: AbortSignalLike,
  ): Promise<ProtocolResponse> {
    return new Promise((resolve, reject) => {
      if (signal?.aborted) {
        reject(abortReason(signal));
        return;
      }
      const socket = createConnection(this.#path);
      // A client-wide close() abandons an in-flight chat: fail it as a
      // disconnect and let close() destroy the socket.
      this.#secondarySockets.set(socket, () => {
        fail(new BackendClientError('disconnected', 'Client closed during chat'));
      });
      const frames = new NewlineFramer();
      let settled = false;
      let onAbort: (() => void) | undefined;
      const connectTimeout = setTimeout(() => {
        fail(
          new BackendClientError(
            'timeout',
            `Timed out connecting to server after ${this.#connectTimeoutMs}ms`,
          ),
        );
      }, this.#connectTimeoutMs);

      const cleanup = (): void => {
        clearTimeout(connectTimeout);
        this.#secondarySockets.delete(socket);
        socket.off('error', fail);
        socket.off('close', disconnected);
        if (signal !== undefined && onAbort !== undefined) {
          signal.removeEventListener('abort', onAbort);
        }
      };
      const fail = (error: Error): void => {
        if (settled) return;
        settled = true;
        cleanup();
        socket.destroy();
        reject(error instanceof BackendClientError ? error : transportFailure(error));
      };
      const disconnected = (): void =>
        fail(new BackendClientError('disconnected', 'Server disconnected during chat'));
      const finish = (response: ProtocolResponse): void => {
        if (settled) return;
        if (response.request_id !== request.request_id) {
          fail(
            new BackendClientError('parse', 'Server chat response has an unexpected request ID'),
          );
          return;
        }
        settled = true;
        cleanup();
        // Keep the one-shot error listener until the socket actually closes.
        // A peer reset during the FIN handshake must not become an unhandled
        // EventEmitter error after the response promise has settled.
        socket.once('error', fail);
        socket.once('close', () => socket.off('error', fail));
        socket.end();
        if (response.ok) resolve(response);
        else reject(responseError(response));
      };
      const handleChatResponseLine = (line: string): boolean => {
        if (!line) return false;
        try {
          finish(parseProtocolResponse(line));
        } catch (error) {
          fail(toError(error));
        }
        return true;
      };

      socket.setEncoding('utf8');
      socket.once('connect', () => {
        clearTimeout(connectTimeout);
        socket.write(`${JSON.stringify(request)}\n`, error => {
          if (error) fail(error);
        });
      });
      socket.on('data', chunk => {
        const lines = frameChunk(frames, chunk.toString(), fail);
        if (lines === null) return;
        for (const line of lines) {
          if (handleChatResponseLine(line)) return;
        }
      });
      socket.once('error', fail);
      socket.once('close', disconnected);
      if (signal !== undefined) {
        // Cancellation tears the dedicated socket down and rejects with the
        // abort reason, bypassing the transport-failure wrap so the caller sees
        // the abort, not a disconnect.
        onAbort = (): void => {
          if (settled) return;
          settled = true;
          cleanup();
          socket.destroy();
          reject(abortReason(signal));
        };
        signal.addEventListener('abort', onAbort, {once: true});
      }
    });
  }

  /** Route one request by the channel's state: send, hold, or fail it. */
  #enqueue(entry: ControlRequest): void {
    if (this.#controlState === 'connected') {
      this.#send(entry);
      return;
    }
    // An outage is in progress: hold an idempotent request to resend once the
    // channel recovers; fail one that must not be repeated so the caller, seeing
    // the disconnected state, decides whether to reissue it.
    if (this.#controlState === 'reconnecting' && entry.policy.idempotent) {
      this.#held.push(entry);
      return;
    }
    this.#settleReject(entry, disconnectedError());
  }

  /** Write one request to the live socket and arm its response deadline. */
  #send(entry: ControlRequest): void {
    const requestId = entry.requestId;
    entry.timeout = setTimeout(() => {
      this.#pending.delete(requestId);
      this.#settleReject(
        entry,
        new BackendClientError('timeout', `Server request timed out after ${entry.timeoutMs}ms`),
      );
    }, entry.timeoutMs);
    this.#pending.set(requestId, entry);
    // Capture the socket: by the time this async callback fires, a drop and
    // redial may have replaced `#socket`, and the stale-socket guard in
    // `#onControlDrop` must see the socket the write was actually issued on, not
    // whatever is live now, or a write error would drop a recovered channel.
    const socket = this.#socket;
    socket.write(`${JSON.stringify(entry.request)}\n`, error => {
      // A write failure means the socket is broken; drive the drop path so the
      // request is disposed by its policy and the redial loop takes over.
      if (error) this.#onControlDrop(socket, transportFailure(error));
    });
  }

  /** Free an abandoned request's slot and reject it; its id is never reused. */
  #abort(entry: ControlRequest, error: Error): void {
    const requestId = entry.requestId;
    if (this.#pending.get(requestId) === entry) this.#pending.delete(requestId);
    const heldIndex = this.#held.indexOf(entry);
    if (heldIndex !== -1) this.#held.splice(heldIndex, 1);
    this.#settleReject(entry, error);
  }

  #settle(entry: ControlRequest): void {
    if (entry.timeout !== null) {
      clearTimeout(entry.timeout);
      entry.timeout = null;
    }
    entry.detachAbort();
  }

  #settleReject(entry: ControlRequest, error: Error): void {
    this.#settle(entry);
    entry.reject(error);
  }

  #settleResolve(entry: ControlRequest, response: ProtocolResponse): void {
    this.#settle(entry);
    entry.resolve(response);
  }

  #attachControlSocket(socket: Socket): void {
    this.#socket = socket;
    // A framer per socket, so a superseded socket's trailing bytes can never
    // splice into the live socket's stream.
    const frames = new NewlineFramer();
    socket.setEncoding('utf8');
    socket.on('data', chunk => this.#onData(frames, chunk.toString(), socket));
    socket.on('error', error => this.#onControlDrop(socket, transportFailure(error)));
    socket.on('close', () =>
      this.#onControlDrop(socket, new BackendClientError('disconnected', 'Server disconnected')),
    );
  }

  #onData(frames: NewlineFramer, chunk: string, socket: Socket): void {
    if (socket !== this.#socket) return;
    const lines = frameChunk(frames, chunk, error => this.#failControl(socket, error));
    if (lines === null) return;
    for (const line of lines) {
      if (!line) continue;
      let response: ProtocolResponse;
      try {
        response = parseProtocolResponse(line);
      } catch (error) {
        this.#failControl(socket, streamFailure(error));
        return;
      }
      const pending = this.#pending.get(response.request_id);
      // No match means a retired, cancelled, or otherwise unknown id: discard
      // it rather than misroute it onto a later request that reused nothing.
      if (!pending) continue;
      this.#pending.delete(response.request_id);
      if (response.ok) this.#settleResolve(pending, response);
      else this.#settleReject(pending, responseError(response));
    }
  }

  /**
   * A protocol fault on the live socket: the peer sent bytes this client cannot
   * read (malformed JSON, an unknown message, an unsupported version, an
   * unframable stream). Unlike a transport drop, this is not a transient outage
   * a redial recovers, and it is a real answer about every in-flight request, so
   * each fails with the typed error rather than being silently resent. The
   * channel settles into the reportable dead state that `reconnect()` can revive
   * if a caller decides the fault was one-off.
   */
  #failControl(socket: Socket, error: Error): void {
    if (socket !== this.#socket || this.#controlState !== 'connected') return;
    this.#controlState = 'disconnected';
    this.#reportDisconnected(error);
    const owed = [...this.#pending.values(), ...this.#held.splice(0)];
    this.#pending.clear();
    for (const entry of owed) this.#settleReject(entry, error);
    socket.destroy();
  }

  /**
   * A live control socket dropped. Transition to reconnecting once (a stale
   * socket's late event or a shutdown is ignored), report the disconnect,
   * dispose the in-flight requests by policy, and start the backoff loop.
   */
  #onControlDrop(socket: Socket, error: Error): void {
    if (socket !== this.#socket || this.#controlState !== 'connected') return;
    this.#controlState = 'reconnecting';
    this.#reportDisconnected(error);
    socket.destroy();
    this.#disposePending();
    this.#backoff.reset();
    this.#scheduleRedial();
  }

  #disposePending(): void {
    for (const entry of this.#pending.values()) {
      if (entry.timeout !== null) {
        clearTimeout(entry.timeout);
        entry.timeout = null;
      }
      // An idempotent request rides the recovery; one that must not repeat fails
      // now with a typed disconnect.
      if (entry.policy.idempotent) this.#held.push(entry);
      else this.#settleReject(entry, disconnectedError());
    }
    this.#pending.clear();
  }

  #scheduleRedial(): void {
    if (this.#redialTimer !== null) return;
    const delayMs = this.#backoff.next();
    if (delayMs === undefined) {
      this.#exhaust();
      return;
    }
    this.#redialTimer = setTimeout(() => {
      this.#redialTimer = null;
      void this.#redialAttempt();
    }, delayMs);
  }

  #exhaust(): void {
    // The finite schedule is spent: no channel is coming without a caller
    // asking for one, so fail every held request and settle into a reportable
    // dead state that `reconnect()` can revive.
    this.#controlState = 'disconnected';
    for (const entry of this.#held.splice(0)) this.#settleReject(entry, disconnectedError());
  }

  async #redialAttempt(): Promise<void> {
    if (this.#controlState !== 'reconnecting' || this.#redialing) return;
    this.#redialing = true;
    try {
      let socket: Socket;
      try {
        socket = await dialSocket(
          this.#path,
          this.#connectTimeoutMs,
          `Timed out connecting to server after ${this.#connectTimeoutMs}ms`,
        );
      } catch {
        // This attempt failed; back off again, or exhaust the schedule.
        if (this.#controlState === 'reconnecting') this.#scheduleRedial();
        return;
      }
      if (this.#controlState !== 'reconnecting') {
        // close() or a revive raced the dial; the new socket is not wanted.
        void closeSocketWithin(socket, this.#closeGraceMs);
        return;
      }
      this.#attachControlSocket(socket);
      this.#controlState = 'connected';
      this.#backoff.reset();
      this.#reportConnected();
      this.#flushHeld();
    } finally {
      this.#redialing = false;
    }
  }

  #flushHeld(): void {
    for (const entry of this.#held.splice(0)) {
      // Mint a fresh id: the id the request last carried was written to a socket
      // that is now destroyed, so reusing it could let a late frame from that
      // dead socket match this resend. A never-reused id closes that off.
      const nextId = globalThis.crypto.randomUUID();
      entry.request = {...entry.request, request_id: nextId};
      entry.requestId = nextId;
      this.#send(entry);
    }
  }

  #reportDisconnected(error: Error): void {
    if (this.#disconnectedReported) return;
    this.#disconnectedReported = true;
    this.#onConnectionState?.({status: 'disconnected', error});
  }

  #reportConnected(): void {
    this.#disconnectedReported = false;
    this.#onConnectionState?.({status: 'connected'});
  }
}

/**
 * Dial the server socket once, resolving with the connected socket or rejecting
 * with a typed dial failure. The one boundary that knows Node errnos, so
 * everything downstream branches on `kind`/`retryable`, never on the code.
 */
function dialSocket(path: string, timeoutMs: number, timeoutMessage: string): Promise<Socket> {
  return new Promise((resolve, reject) => {
    const socket = createConnection(path);
    const onError = (error: Error): void => {
      clearTimeout(timer);
      reject(dialFailure(error));
    };
    const timer = setTimeout(() => {
      socket.destroy();
      reject(new BackendClientError('timeout', timeoutMessage));
    }, timeoutMs);
    socket.once('connect', () => {
      clearTimeout(timer);
      socket.off('error', onError);
      resolve(socket);
    });
    socket.once('error', onError);
  });
}

/**
 * Classify one dial attempt's failure: only the errors a still-starting server
 * produces are transient. Everything downstream branches on `kind` and
 * `retryable`, never on the code.
 */
function dialFailure(error: Error): BackendClientError {
  const code = (error as {code?: string}).code;
  return new BackendClientError('disconnected', error.message, {
    retryable: code !== undefined && RETRYABLE_CONNECT_CODES.has(code),
    cause: error,
  });
}

function isTransientDialError(error: unknown): error is BackendClientError {
  return error instanceof BackendClientError && error.kind === 'disconnected' && error.retryable;
}

/** A live connection failed under an operation; the server said nothing. */
function transportFailure(error: Error): BackendClientError {
  return new BackendClientError('disconnected', error.message, {cause: error});
}

/** A typed disconnect for a request the client cannot carry right now. */
function disconnectedError(message = 'Server is disconnected'): BackendClientError {
  return new BackendClientError('disconnected', message);
}

/**
 * The error an abort rejects with: the signal's reason when it is an `Error`
 * (the caller's own), otherwise a standard `AbortError`. Kept out of the
 * transport-failure taxonomy so a caller-initiated cancel is never mistaken for
 * a disconnect.
 */
function abortReason(signal: AbortSignalLike): Error {
  const reason = signal.reason;
  if (reason instanceof Error) return reason;
  const error = new Error(typeof reason === 'string' && reason ? reason : 'Request aborted');
  error.name = 'AbortError';
  return error;
}

/**
 * Pass a typed stream failure through; anything else escaped from processing a
 * line (a consumer callback throw included), so the stream could not be read.
 */
function streamFailure(error: unknown): BackendClientError {
  if (error instanceof BackendClientError) return error;
  const cause = toError(error);
  return new BackendClientError('parse', cause.message, {cause});
}

/**
 * Frame one socket chunk, or report an unreadable stream. Returns the completed
 * lines, or null when the framer rejects the chunk (an oversized newline-less
 * remainder), having first handed the typed failure to `onFramingError` so the
 * caller can tear its connection down.
 */
function frameChunk(
  framer: NewlineFramer,
  chunk: string,
  onFramingError: (error: BackendClientError) => void,
): string[] | null {
  try {
    return framer.push(chunk);
  } catch (error) {
    onFramingError(streamFailure(error));
    return null;
  }
}

function delay(ms: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, ms));
}

function toError(error: unknown): Error {
  return error instanceof Error ? error : new Error(String(error));
}

function responseError(response: ProtocolResponse): ServerError {
  return new ServerError(response.error ?? 'Unknown server error', response.diagnostic ?? null);
}

/**
 * End a socket gracefully, then force it closed if the peer does not complete
 * the FIN handshake within `graceMs`. Resolves once the socket is actually
 * closed (whether it ended or was destroyed), so a caller awaiting close cannot
 * hang on an unresponsive server. The grace timer does not keep the event loop
 * alive on its own.
 */
function closeSocketWithin(socket: Socket, graceMs: number): Promise<void> {
  return new Promise(resolve => {
    if (socket.destroyed) return resolve();
    const timer = setTimeout(() => socket.destroy(), graceMs);
    timer.unref?.();
    socket.once('close', () => {
      clearTimeout(timer);
      resolve();
    });
    socket.end();
  });
}

function parseProtocolResponse(line: string): ProtocolResponse {
  const value = parseRecord(line, 'response');
  if (value['protocol_version'] !== 1) {
    throw new BackendClientError('parse', 'Unsupported server protocol version');
  }
  if (typeof value['request_id'] !== 'string') {
    throw new BackendClientError('parse', 'Invalid server response: request_id must be a string');
  }
  if (typeof value['ok'] !== 'boolean') {
    throw new BackendClientError('parse', 'Invalid server response: ok must be a boolean');
  }
  return value as unknown as ProtocolResponse;
}

function parseServerMessage(line: string): ServerMessage {
  const value = parseRecord(line, 'event-stream message');
  const type = value['type'];
  if (type === 'subscribed') {
    if (
      typeof value['request_id'] !== 'string' ||
      typeof value['run_id'] !== 'string' ||
      typeof value['latest_sequence'] !== 'number'
    ) {
      throw new BackendClientError('parse', 'Invalid subscribed message');
    }
  } else if (type === 'event') {
    if (!isRecord(value['event'])) throw new BackendClientError('parse', 'Invalid event message');
  } else if (type === 'event_batch') {
    if (!Array.isArray(value['events'])) {
      throw new BackendClientError('parse', 'Invalid event batch message');
    }
  } else if (type === 'protocol_error') {
    if (typeof value['code'] !== 'string' || typeof value['message'] !== 'string') {
      throw new BackendClientError('parse', 'Invalid protocol error message');
    }
  } else {
    throw unknownStreamLineError(value);
  }
  return value as unknown as ServerMessage;
}

/**
 * A server that predates a subscribe field rejects the subscription the way it
 * rejects any request: with a `Response` line on the stream socket, `ok: false`
 * and no `type`. That line is the server refusing the request, the expected
 * answer to a capability probe, not a line the protocol cannot read; anything
 * else without a known `type` is one the protocol cannot read.
 */
function unknownStreamLineError(value: Record<string, unknown>): BackendClientError {
  const rejected =
    value['type'] === undefined && value['ok'] === false && typeof value['request_id'] === 'string';
  if (!rejected) {
    return new BackendClientError(
      'parse',
      `Unknown server event-stream message: ${String(value['type'])}`,
    );
  }
  return new ServerError(
    typeof value['error'] === 'string' ? value['error'] : 'Server rejected the subscription',
    isRecord(value['diagnostic']) ? (value['diagnostic'] as unknown as Diagnostic) : null,
  );
}

function parseRecord(line: string, description: string): Record<string, unknown> {
  let value: unknown;
  try {
    value = JSON.parse(line);
  } catch (error) {
    throw new BackendClientError(
      'parse',
      `Invalid server ${description} JSON: ${error instanceof Error ? error.message : String(error)}`,
      {cause: error},
    );
  }
  if (!isRecord(value)) {
    throw new BackendClientError('parse', `Invalid server ${description}: expected an object`);
  }
  return value;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}
