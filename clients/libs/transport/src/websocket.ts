import {
  BackendClientError,
  type ProtocolResponse,
  parseProtocolResponse,
  parseServerMessage,
  type RequestInput,
  responseError,
  ServerError,
  type ServerMessage,
  streamFailure,
} from '@vibesys/contracts';
import type {RunChannelRole} from './channel-role.js';
import {
  ControlChannel,
  type ControlChannelState,
  type ControlConnection,
  type ControlConnectionHandlers,
  defaultScheduleTimeout,
  type IssuedRequest,
  type ScheduleTimeout,
} from './control-channel.js';
import {type AbortSignalLike, abortReason, type RequestOptions} from './request-policy.js';
import {
  type ControlTransport,
  type EventSubscription,
  type SubscribeOptions,
  subscribeRequest,
} from './transport.js';

const OPEN = 1;
const CLOSED = 3;
const DEFAULT_CONNECT_TIMEOUT_MS = 5_000;
const DEFAULT_CLOSE_GRACE_MS = 250;

/** The small DOM surface the transport needs, injectable for deterministic tests. */
export interface WebSocketLike {
  readonly readyState: number;
  onopen: (() => void) | null;
  onmessage: ((event: {readonly data: unknown}) => void) | null;
  onerror: (() => void) | null;
  onclose: (() => void) | null;
  send(data: string): void;
  close(code?: number, reason?: string): void;
}

export interface WebSocketTransportOptions {
  /** Stable frontend identity reflected by server acknowledgements. */
  clientId?: string;
  connectTimeoutMs?: number;
  requestTimeoutMs?: number;
  closeGraceMs?: number;
  /**
   * Backoff schedule for control-channel redials after a failed dial or a
   * drop; its length bounds the attempts per outage, so a gateway that is down
   * costs one bounded series of dials rather than a fresh connect timeout per
   * operator action. Defaults to the schedule the subscription shares.
   */
  reconnectDelaysMs?: readonly number[];
  /**
   * Observe control-channel connectivity: `disconnected` when a dial fails or
   * a live channel drops, `connected` when one comes up. Reported once per
   * transition, so a frontend can disable the controls a dead channel cannot
   * carry instead of rendering them as live.
   */
  onConnectionState?: (state: ControlChannelState) => void;
  /** Override the browser constructor in tests or an embedded web runtime. */
  webSocket?: (url: string, role: RunChannelRole) => WebSocketLike;
  /** Override timeout scheduling in deterministic tests. */
  scheduleTimeout?: ScheduleTimeout;
}

/**
 * Browser transport for the protocol's three connection roles. WebSocket
 * frames are already message-delimited, so no Node stream or newline framer
 * reaches this entry point.
 *
 * Control-request policy is not this class's business: the runtime-neutral
 * `ControlChannel` owns the redial schedule, the disposition of requests in
 * flight when a socket drops, the per-type idempotency table, cancellation,
 * and the connectivity a frontend reads, so the browser and the Node client
 * answer all of it identically. This class supplies the sockets.
 *
 * It dials lazily, so the control channel opens on the first request rather
 * than in the constructor. That is the one place the browser's story differs
 * from the Node client's, and the channel handles it as cold start: a first
 * request waits for its dial whatever its type, because nothing has reported a
 * failure yet. A later outage is a different situation and gets the outage
 * rule. See `ControlChannel`'s `#enqueue`.
 */
export class WebSocketTransport implements ControlTransport {
  readonly #url: string;
  readonly #clientId: string;
  readonly #connectTimeoutMs: number;
  readonly #closeGraceMs: number;
  readonly #webSocket: (url: string, role: RunChannelRole) => WebSocketLike;
  /**
   * Every socket a consumer has claimed, mapped to the hook that settles its
   * current operation. This includes the control connection: retaining it here
   * closes the promise-handoff window before `ControlChannel` adopts it.
   * `close()` settles each owner before tearing its socket down.
   */
  readonly #claimedSockets = new Map<WebSocketLike, () => void>();
  /**
   * Sockets not yet claimed by a caller, with the one callback that settles
   * their handshake and releases them. Keeping settlement beside ownership
   * makes client-wide close participate in the same fence as browser events,
   * timeouts, and request cancellation.
   */
  readonly #openingSockets = new Map<WebSocketLike, () => Promise<void>>();
  /** One physical close per socket, including closes already in progress. */
  readonly #socketCloses = new WeakMap<WebSocketLike, Promise<void>>();
  /** In-progress closes that a later client-wide close must also join. */
  readonly #closingSockets = new Set<Promise<void>>();
  readonly #scheduleTimeout: ScheduleTimeout;
  readonly #channel: ControlChannel;

  #closed = false;

  constructor(url: string, options: WebSocketTransportOptions = {}) {
    this.#url = url;
    this.#clientId = options.clientId ?? globalThis.crypto.randomUUID();
    this.#connectTimeoutMs = options.connectTimeoutMs ?? DEFAULT_CONNECT_TIMEOUT_MS;
    this.#closeGraceMs = options.closeGraceMs ?? DEFAULT_CLOSE_GRACE_MS;
    this.#webSocket = options.webSocket ?? defaultWebSocket;
    this.#scheduleTimeout = options.scheduleTimeout ?? defaultScheduleTimeout;
    this.#channel = new ControlChannel(
      {
        open: handlers => this.#openControl(handlers),
        runDedicated: (request, signal) => this.#requestDedicated(request, signal),
      },
      {
        clientId: this.#clientId,
        // Passed through rather than defaulted here: the channel owns the
        // request deadline, so the 30s fallback has one definition.
        requestTimeoutMs: options.requestTimeoutMs,
        reconnectDelaysMs: options.reconnectDelaysMs,
        onConnectionState: options.onConnectionState,
        scheduleTimeout: this.#scheduleTimeout,
      },
    );
  }

  /**
   * Send a control request and resolve with the server's response. See
   * `ControlChannel.request`: `options` carry the per-call deadline,
   * connection, and `AbortSignal`, and the request type's table entry
   * (`request-policy.ts`) supplies the rest.
   */
  request(input: RequestInput, options: RequestOptions = {}): Promise<ProtocolResponse> {
    return this.#channel.request(input, options);
  }

  /**
   * Dial the control channel now, whatever its backoff schedule was going to
   * do. See `ControlChannel.reconnect`.
   */
  reconnect(): void {
    this.#channel.reconnect();
  }

  /** Whether the control channel currently holds a live connection. */
  get connected(): boolean {
    return this.#channel.connected;
  }

  async subscribe(
    afterSequence: number,
    onMessage: (message: ServerMessage) => void,
    onDisconnect: (error: Error) => void,
    options: SubscribeOptions = {},
  ): Promise<EventSubscription> {
    if (this.#closed) throw disconnectedError('Client is closed');
    const request = this.#envelope(subscribeRequest(afterSequence, options));
    const socket = await this.#openSocket('subscribe');
    let settleClaimed = (): void => {};
    this.#claim(socket, 'Server disconnected before the subscription opened', () =>
      settleClaimed(),
    );
    return new Promise((resolve, reject) => {
      let subscribed = false;
      let closing = false;
      let disconnected = false;
      let protocolErrorReceived = false;
      let cancelHandshake = (): void => {};
      const disconnect = (error: Error): void => {
        if (disconnected || closing) return;
        disconnected = true;
        cancelHandshake();
        this.#claimedSockets.delete(socket);
        if (subscribed && !protocolErrorReceived) onDisconnect(error);
        else if (!subscribed) reject(error);
      };
      const settle = (): void => {
        cancelHandshake();
        if (subscribed) {
          closing = true;
          return;
        }
        if (disconnected) return;
        disconnected = true;
        reject(disconnectedError('Client closed during subscription'));
      };
      settleClaimed = settle;
      // One handler owns the accepted-subscription state machine, including
      // the protocol-error-before-close rule. Keeping it together prevents
      // the browser and Node adapters from drifting on ordering semantics.
      socket.onmessage = event => {
        try {
          const message = parseServerMessage(textFrame(event.data));
          onMessage(message);
          if (message.type === 'protocol_error') {
            protocolErrorReceived = true;
            if (!subscribed) {
              disconnect(new ServerError(message.message, message.diagnostic ?? null));
              void this.#closeSocket(socket);
              return;
            }
          }
          if (!subscribed && message.type === 'subscribed') {
            subscribed = true;
            cancelHandshake();
            resolve({
              close: async () => {
                closing = true;
                cancelHandshake();
                this.#claimedSockets.delete(socket);
                await this.#closeSocket(socket);
              },
            });
          }
        } catch (error) {
          const failure = streamFailure(error);
          disconnect(failure);
          void this.#closeSocket(socket);
        }
      };
      socket.onerror = () => {
        disconnect(transportFailure(new Error('WebSocket transport error')));
        void this.#closeSocket(socket);
      };
      socket.onclose = () => {
        this.#claimedSockets.delete(socket);
        disconnect(
          disconnectedError(
            subscribed ? 'Server event stream disconnected' : 'Server rejected the subscription',
          ),
        );
      };
      const scheduledCancel = this.#scheduleTimeout(() => {
        disconnect(
          new BackendClientError(
            'timeout',
            `Server subscription timed out after ${this.#connectTimeoutMs}ms`,
          ),
        );
        void this.#closeSocket(socket);
      }, this.#connectTimeoutMs);
      cancelHandshake = scheduledCancel;
      if (disconnected || closing) {
        scheduledCancel();
        return;
      }
      try {
        socket.send(JSON.stringify(request));
      } catch (error) {
        disconnect(transportFailure(error));
        void this.#closeSocket(socket);
      }
    });
  }

  /**
   * Close every socket the transport owns: every claimed connection and any
   * socket still handshaking. Fails every control request still owed an answer,
   * so nothing is left waiting on a client that is gone.
   *
   * A control socket is covered in both of its states. A live one is closed by
   * `ControlChannel.close`, which this awaits; one whose dial is in flight, or
   * which has resolved but not yet been claimed, is closed here as an opening
   * socket. So there is no interleaving that returns from `close()` with a
   * socket still open.
   */
  async close(): Promise<void> {
    this.#closed = true;
    const channelClosed = this.#channel.close();
    const claimed = [...this.#claimedSockets.entries()];
    this.#claimedSockets.clear();
    for (const [, settle] of claimed) settle();
    const closing = [...this.#closingSockets];
    for (const settle of [...this.#openingSockets.values()]) closing.push(settle());
    for (const [socket] of claimed) closing.push(this.#closeSocket(socket));
    // A test or embedded constructor can call `close()` reentrantly before it
    // returns its socket to `#openSocket`. Give that stack one microtask to
    // publish the socket, then join the close it starts on seeing `#closed`.
    await Promise.resolve();
    for (const settle of [...this.#openingSockets.values()]) closing.push(settle());
    closing.push(...this.#closingSockets);
    await Promise.all([channelClosed, ...closing]);
  }

  /**
   * Transfer a freshly dialed socket to its next owner, or dispose of it and
   * throw. The transfer is synchronous: there is no promise continuation in
   * which neither the opening registry nor the claimed registry owns it.
   *
   * `#openSocket` resolves with the socket still registered as opening, so this
   * is where it stops being the transport's teardown responsibility and starts
   * being the caller's. Two things can have happened in the microtask between
   * the dial settling and the caller running, and both are checked here rather
   * than assumed away:
   *
   * `close()` may have landed, which the pre-existing check covers. And the
   * socket itself may already be gone, because a socket handed back by an
   * injected factory that was open on arrival carries no handlers at all, and
   * even the handshake path's handlers no-op once settled. Either way the event
   * reached no listener, so `readyState` is the only surviving evidence. Read
   * before the `delete`, so an interleaving `close()` cannot make a live socket
   * look dead.
   *
   * A dead socket fails the dial rather than reporting a drop, because the
   * caller has not installed its handlers yet: the control channel treats a
   * failed dial as an outage and arms its backoff, which is the same recovery a
   * drop would get.
   */
  #claim(socket: WebSocketLike, lostMessage: string, settle: () => void): void {
    const live = socket.readyState === OPEN;
    if (this.#closed) {
      this.#openingSockets.delete(socket);
      void this.#closeSocket(socket);
      throw disconnectedError('Client is closed');
    }
    if (!live) {
      this.#openingSockets.delete(socket);
      void this.#closeSocket(socket);
      throw disconnectedError(lostMessage);
    }
    this.#claimedSockets.set(socket, settle);
    this.#openingSockets.delete(socket);
  }

  /** Open one control socket for the channel and route its frames back. */
  async #openControl(handlers: ControlConnectionHandlers): Promise<ControlConnection> {
    const socket = await this.#openSocket('run-control');
    const connection: ControlConnection = {
      send: frame => {
        try {
          socket.send(frame);
        } catch (error) {
          // A send that throws means the socket is broken; report it as a drop
          // so the request is disposed by its policy and the redial takes over.
          handlers.onDrop(transportFailure(error));
        }
      },
      close: async () => {
        this.#claimedSockets.delete(socket);
        socket.onmessage = null;
        await this.#closeSocket(socket);
      },
    };
    this.#claim(socket, 'Server disconnected before the control channel opened', () => {
      socket.onmessage = null;
      socket.onerror = null;
      socket.onclose = null;
    });
    socket.onmessage = event => {
      const data = event.data;
      // A binary frame on a text protocol is a fault, not an outage: redialing
      // would get the same bytes back.
      if (typeof data !== 'string') {
        handlers.onFault(new BackendClientError('parse', 'WebSocket frame must be text'));
        return;
      }
      handlers.onFrame(data);
    };
    socket.onerror = () =>
      handlers.onDrop(transportFailure(new Error('WebSocket transport error')));
    socket.onclose = () => {
      this.#claimedSockets.delete(socket);
      handlers.onDrop(disconnectedError('Server disconnected'));
    };
    return connection;
  }

  /**
   * Dial one socket and resolve once it is open.
   *
   * The socket stays in `#openingSockets` across the resolution, so a `close()`
   * that lands before the caller has adopted it still tears it down instead of
   * leaking a live connection past the client's own teardown. Every caller
   * hands it to `#claim`, which is what moves the responsibility across.
   *
   * A dedicated request also passes its abort signal here. Cancellation owns
   * the still-opening socket immediately rather than waiting for the browser's
   * connect timeout; the settled fence makes a queued late socket event inert.
   */
  async #openSocket(role: RunChannelRole, signal?: AbortSignalLike): Promise<WebSocketLike> {
    if (signal?.aborted) throw abortReason(signal);
    let socket: WebSocketLike;
    try {
      socket = this.#webSocket(this.#url, role);
    } catch (error) {
      throw transportFailure(error);
    }
    // An injected factory may hand back a socket that is already open, in which
    // case there is no handshake to wait on and no `onopen` to come.
    if (socket.readyState === OPEN) {
      const closeOpening = (): Promise<void> => {
        this.#openingSockets.delete(socket);
        return this.#closeSocket(socket);
      };
      this.#openingSockets.set(socket, closeOpening);
      if (this.#closed) {
        await closeOpening();
        throw disconnectedError('Client is closed');
      }
      if (!signal?.aborted) return socket;
      this.#openingSockets.delete(socket);
      await this.#closeSocket(socket);
      throw abortReason(signal);
    }
    return new Promise((resolve, reject) => {
      let settled = false;
      let cancelTimeout = (): void => {};
      let detachAbort = (): void => {};
      const finish = (): boolean => {
        if (settled) return false;
        settled = true;
        cancelTimeout();
        detachAbort();
        return true;
      };
      const fail = (error: Error, close: boolean): void => {
        if (!finish()) return;
        this.#openingSockets.delete(socket);
        if (close) void this.#closeSocket(socket);
        reject(error);
      };
      const closeOpening = (): Promise<void> => {
        if (finish()) reject(disconnectedError('Client closed during WebSocket handshake'));
        this.#openingSockets.delete(socket);
        return this.#closeSocket(socket);
      };
      this.#openingSockets.set(socket, closeOpening);
      // The injected constructor is allowed to re-enter the transport. If it
      // closed the client while returning this socket, adopt that terminal
      // state before installing any browser callbacks or timers.
      if (this.#closed) {
        void closeOpening();
        return;
      }
      socket.onopen = () => {
        if (!finish()) return;
        // Left registered on purpose: `#claim` deregisters it once a caller has
        // installed its own handlers.
        resolve(socket);
      };
      socket.onerror = () => fail(transportFailure(new Error('WebSocket transport error')), true);
      socket.onclose = () =>
        fail(disconnectedError('Server disconnected before WebSocket opened'), false);
      const scheduledCancel = this.#scheduleTimeout(
        () =>
          fail(
            new BackendClientError(
              'timeout',
              `Timed out connecting to server after ${this.#connectTimeoutMs}ms`,
            ),
            true,
          ),
        this.#connectTimeoutMs,
      );
      cancelTimeout = scheduledCancel;
      // A structural scheduler may invoke code reentrantly before returning
      // its cancellation handle. Do not leave that newly returned timer live
      // if another terminal path already won.
      if (settled) scheduledCancel();
      if (signal !== undefined) {
        const onAbort = (): void => fail(abortReason(signal), true);
        detachAbort = () => signal.removeEventListener('abort', onAbort);
        signal.addEventListener('abort', onAbort, {once: true});
        // Likewise, client-wide close can re-enter from a structural signal's
        // listener registration. Remove a listener installed after settlement.
        if (settled) detachAbort();
        // A structural signal can abort while the listener is being attached.
        if (signal.aborted) onAbort();
      }
    });
  }

  /**
   * Run one request on its own socket with no response deadline.
   *
   * A chat is bounded by the agent it drives, not by the control RPC timeout,
   * and a long one must not block pause, resume, or snapshot behind it, so it
   * gets a socket of its own. Which requests come here is the policy table's
   * decision, not this transport's.
   */
  async #requestDedicated(
    request: IssuedRequest,
    signal?: AbortSignalLike,
  ): Promise<ProtocolResponse> {
    const socket = await this.#openSocket(
      request.type === 'query.chat' ? 'chat' : 'run-control',
      signal,
    );
    if (signal?.aborted) {
      // Re-checked after the dial: cancellation can land in the microtask gap
      // between the handshake settling (which detaches its listener) and this
      // caller claiming the socket.
      this.#openingSockets.delete(socket);
      void this.#closeSocket(socket);
      throw abortReason(signal);
    }
    return this.#dedicatedResponse(socket, request, signal);
  }

  /** Await the one response a dedicated socket owes, then release the socket. */
  #dedicatedResponse(
    socket: WebSocketLike,
    request: IssuedRequest,
    signal: AbortSignalLike | undefined,
  ): Promise<ProtocolResponse> {
    return new Promise((resolve, reject) => {
      let settled = false;
      let detachAbort = (): void => {};
      const finish = (callback: () => void): void => {
        if (settled) return;
        settled = true;
        detachAbort();
        this.#claimedSockets.delete(socket);
        socket.onmessage = null;
        callback();
        void this.#closeSocket(socket);
      };
      const settle = (): void => {
        if (settled) return;
        settled = true;
        detachAbort();
        socket.onmessage = null;
        reject(disconnectedError('Client closed during chat'));
      };
      try {
        this.#claim(socket, 'Server disconnected before the chat socket opened', settle);
      } catch (error) {
        reject(error);
        return;
      }
      if (signal !== undefined) {
        // Cancellation tears the dedicated socket down and rejects with the
        // abort reason, so the caller sees the abort, not a disconnect.
        const onAbort = (): void => finish(() => reject(abortReason(signal)));
        detachAbort = () => signal.removeEventListener('abort', onAbort);
        signal.addEventListener('abort', onAbort, {once: true});
        if (settled) {
          detachAbort();
          return;
        }
        // A structural signal can become aborted while its listener is being
        // attached without dispatching. Preserve the signal contract anyway.
        if (signal.aborted) onAbort();
        if (settled) return;
      }
      socket.onmessage = event => {
        try {
          const response = parseProtocolResponse(textFrame(event.data));
          if (response.request_id !== request.request_id) {
            finish(() =>
              reject(
                new BackendClientError(
                  'parse',
                  'Server chat response has an unexpected request ID',
                ),
              ),
            );
            return;
          }
          finish(() => (response.ok ? resolve(response) : reject(responseError(response))));
        } catch (error) {
          finish(() => reject(streamFailure(error)));
        }
      };
      socket.onerror = () =>
        finish(() => reject(transportFailure(new Error('WebSocket transport error'))));
      socket.onclose = () =>
        finish(() => reject(disconnectedError('Server disconnected during chat')));
      try {
        socket.send(JSON.stringify(request));
      } catch (error) {
        finish(() => reject(transportFailure(error)));
      }
    });
  }

  #envelope(input: RequestInput): IssuedRequest {
    return {
      protocol_version: 1,
      request_id: globalThis.crypto.randomUUID(),
      client_id: this.#clientId,
      timestamp: new Date().toISOString(),
      ...input,
    } as IssuedRequest;
  }

  #closeSocket(socket: WebSocketLike): Promise<void> {
    const existing = this.#socketCloses.get(socket);
    if (existing !== undefined) return existing;

    // Register ownership before calling the browser. A fake, an embedded
    // runtime, or an already-closing socket may report `close` synchronously
    // and re-enter another shutdown path from that callback.
    let complete = (): void => {};
    const closing = new Promise<void>(resolve => {
      complete = resolve;
    });
    this.#socketCloses.set(socket, closing);
    this.#closingSockets.add(closing);
    void closeSocket(socket, this.#closeGraceMs, this.#scheduleTimeout).then(complete);
    void closing.then(() => this.#closingSockets.delete(closing));
    return closing;
  }
}

function defaultWebSocket(url: string, _role: RunChannelRole): WebSocketLike {
  if (typeof globalThis.WebSocket !== 'function') {
    throw new BackendClientError('disconnected', 'WebSocket is unavailable in this runtime', {
      retryable: false,
    });
  }
  return new globalThis.WebSocket(url) as unknown as WebSocketLike;
}

function textFrame(data: unknown): string {
  if (typeof data !== 'string')
    throw new BackendClientError('parse', 'WebSocket frame must be text');
  return data;
}

function disconnectedError(message: string): BackendClientError {
  return new BackendClientError('disconnected', message);
}

function transportFailure(error: unknown): BackendClientError {
  const cause = error instanceof Error ? error : new Error(String(error));
  return new BackendClientError('disconnected', cause.message, {cause});
}

function closeSocket(
  socket: WebSocketLike,
  graceMs: number,
  schedule: ScheduleTimeout,
): Promise<void> {
  return new Promise(resolve => {
    if (socket.readyState === CLOSED) {
      resolve();
      return;
    }
    let settled = false;
    let cancelTimeout = (): void => {};
    const previousOnClose = socket.onclose;
    const finish = (): void => {
      if (settled) return;
      settled = true;
      cancelTimeout();
      resolve();
    };
    cancelTimeout = schedule(finish, graceMs);
    socket.onclose = () => {
      previousOnClose?.();
      finish();
    };
    try {
      socket.close(1000, 'client closed');
    } catch {
      finish();
    }
  });
}
