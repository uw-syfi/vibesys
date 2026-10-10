import {createConnection, type Socket} from 'node:net';
import {
  ControlChannel,
  type ControlChannelState,
  type ControlConnection,
  type ControlConnectionHandlers,
  defaultScheduleTimeout,
  type IssuedRequest,
  type ScheduleTimeout,
} from '../control-channel.js';
import {BackendClientError, ServerError} from '../errors.js';
import {NewlineFramer} from '../newline-framer.js';
import type {ProtocolResponse, RequestInput, ServerMessage} from '../protocol.js';
import {
  parseProtocolResponse,
  parseServerMessage,
  responseError,
  streamFailure,
} from '../protocol-parse.js';
import {type AbortSignalLike, abortReason, type RequestOptions} from '../request-policy.js';
import {type EventSubscription, type SubscribeOptions, subscribeRequest} from '../transport.js';

export interface ServerClientOptions {
  /** Stable frontend identity reflected by server acknowledgements. */
  clientId?: string;
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
  /** Clock and scheduler seam used by every transport deadline and retry. */
  clock?: ClientClock;
}

/**
 * The time source used by the Node transport.
 *
 * Keeping the reading and scheduling sides together makes a connect deadline
 * coherent: a Fake can advance both with one causal operation, while
 * production uses the system clock and timer queue.
 */
export interface ClientClock {
  now(): number;
  readonly scheduleTimeout: ScheduleTimeout;
}

const SYSTEM_CLOCK: ClientClock = {
  now: () => Date.now(),
  scheduleTimeout: defaultScheduleTimeout,
};

const DEFAULT_CONNECT_TIMEOUT_MS = 5_000;
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
 * The Node unix-socket transport: one control channel, plus a socket per
 * subscription and per dedicated request.
 *
 * Everything that decides what happens to a control request (policy, redial,
 * in-flight disposition, cancellation, connectivity reporting) lives in the
 * runtime-neutral `ControlChannel`; this class supplies the sockets and the
 * newline framing it runs over, so the browser transport answers those
 * questions the same way.
 */
export class ServerClient {
  readonly #path: string;
  readonly #clientId: string;
  readonly #connectTimeoutMs: number;
  readonly #closeGraceMs: number;
  readonly #clock: ClientClock;
  /**
   * Every secondary socket the client has opened (subscriptions and dedicated
   * requests), mapped to a hook that settles its own operation. `close()` calls
   * the hook before destroying, so a pending operation fails while tearing a
   * live subscription down does not surface as a spurious stream disconnect,
   * whatever order the caller closes the stream and the client in.
   */
  readonly #secondarySockets = new Map<Socket, () => void>();
  /** Control dials created by a reconnect but not yet handed to the channel. */
  readonly #openingControlDials = new Map<Socket, SocketDial>();
  readonly #channel: ControlChannel;

  #closed = false;
  #closing: Promise<void> | null = null;

  private constructor(socket: Socket, path: string, options: ServerClientOptions) {
    this.#path = path;
    this.#clientId = options.clientId ?? globalThis.crypto.randomUUID();
    this.#connectTimeoutMs = options.connectTimeoutMs ?? DEFAULT_CONNECT_TIMEOUT_MS;
    this.#closeGraceMs = options.closeGraceMs ?? DEFAULT_CLOSE_GRACE_MS;
    this.#clock = options.clock ?? SYSTEM_CLOCK;
    this.#channel = new ControlChannel(
      {
        open: handlers => this.#dialControl(handlers),
        runDedicated: (request, signal) => this.#requestLongRunning(request, signal),
      },
      {
        clientId: this.#clientId,
        requestTimeoutMs: options.requestTimeoutMs,
        reconnectDelaysMs: options.reconnectDelaysMs,
        onConnectionState: options.onConnectionState,
        scheduleTimeout: this.#clock.scheduleTimeout,
      },
    );
    // `connect()` already dialed, so the channel starts connected rather than
    // waiting for the first request to open it.
    this.#channel.adopt(handlers => this.#controlConnection(socket, handlers));
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
    const clock = options.clock ?? SYSTEM_CLOCK;
    const deadline = clock.now() + timeoutMs;
    let lastError: Error | undefined;
    while (true) {
      try {
        return await ServerClient.#connectOnce(path, options, deadline, clock);
      } catch (error) {
        if (!isTransientDialError(error)) throw error;
        lastError = error;
      }
      if (clock.now() + retryIntervalMs >= deadline) {
        throw new BackendClientError(
          'timeout',
          `Timed out connecting to server after ${timeoutMs}ms: ${lastError?.message}`,
          {cause: lastError},
        );
      }
      await delay(retryIntervalMs, clock.scheduleTimeout);
    }
  }

  static #connectOnce(
    path: string,
    options: ServerClientOptions,
    deadline: number,
    clock: ClientClock,
  ): Promise<ServerClient> {
    const configured = options.connectTimeoutMs ?? DEFAULT_CONNECT_TIMEOUT_MS;
    return dialSocket(
      path,
      Math.max(0, deadline - clock.now()),
      `Timed out connecting to server after ${configured}ms`,
      clock.scheduleTimeout,
    ).connected.then(socket => new ServerClient(socket, path, options));
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

  subscribe(
    afterSequence: number,
    onMessage: (message: ServerMessage) => void,
    onDisconnect: (error: Error) => void,
    options: SubscribeOptions = {},
  ): Promise<EventSubscription> {
    if (this.#closed) {
      return Promise.reject(new BackendClientError('disconnected', 'Client is closed'));
    }
    return new Promise((resolve, reject) => {
      const socket = createConnection(this.#path);
      const frames = new NewlineFramer();
      let subscribed = false;
      let closing = false;
      let disconnected = false;
      let protocolErrorReceived = false;
      let cancelHandshakeTimeout = (): void => {};
      cancelHandshakeTimeout = this.#clock.scheduleTimeout(() => {
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
        cancelHandshakeTimeout();
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
          cancelHandshakeTimeout();
          resolve({
            close: () => {
              closing = true;
              cancelHandshakeTimeout();
              this.#secondarySockets.delete(socket);
              return closeSocketWithin(socket, this.#closeGraceMs, this.#clock.scheduleTimeout);
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
      // Track the socket so close() can settle a pending handshake or suppress
      // the disconnect callback of a subscription that is already live.
      this.#secondarySockets.set(socket, () => {
        cancelHandshakeTimeout();
        if (subscribed) {
          closing = true;
          return;
        }
        disconnect(new BackendClientError('disconnected', 'Client closed during subscription'));
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
      onPeerEnd(socket, () => {
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

  /**
   * Send the subscribe request once the subscription socket connects. The frame
   * body is `subscribeRequest`'s typed union member, which owns which optional
   * fields ride along and why.
   */
  #writeSubscribe(socket: Socket, afterSequence: number, options: SubscribeOptions): void {
    const request: IssuedRequest = {
      protocol_version: 1,
      request_id: globalThis.crypto.randomUUID(),
      client_id: this.#clientId,
      timestamp: new Date().toISOString(),
      ...subscribeRequest(afterSequence, options),
    };
    socket.write(`${JSON.stringify(request)}\n`);
  }

  /**
   * Redial the control channel now, whatever its backoff schedule was going to
   * do. See `ControlChannel.reconnect`.
   */
  reconnect(): void {
    this.#channel.reconnect();
  }

  /**
   * Whether the control channel currently holds a live connection. False while
   * reconnecting after an outage, and once the schedule is spent, so a frontend
   * can disable the controls a dropped channel cannot carry. `onConnectionState`
   * reports the same transitions as they happen.
   */
  get connected(): boolean {
    return this.#channel.connected;
  }

  /**
   * Close every socket the client owns: the control connection and every live
   * or opening control socket, plus every live secondary (subscription or
   * dedicated request). Cancels a pending redial, fails every request still
   * owed an answer (in flight or held for resend), and ends each live socket
   * gracefully, destroying it if it does not close within the grace window,
   * so an unresponsive server cannot hang shutdown.
   */
  close(): Promise<void> {
    if (this.#closing !== null) return this.#closing;
    // Mark the whole client closed before failing channel work or settling
    // secondary operations, so no reentrant caller can allocate another socket.
    this.#closed = true;
    this.#closing = this.#closeOwnedSockets();
    return this.#closing;
  }

  /** Perform the one terminal teardown every close caller joins. */
  #closeOwnedSockets(): Promise<void> {
    const channelClosed = this.#channel.close();
    const openingControls = [...this.#openingControlDials.values()];
    this.#openingControlDials.clear();
    const secondaries = [...this.#secondarySockets];
    this.#secondarySockets.clear();
    for (const [, settle] of secondaries) settle();
    return Promise.all([
      channelClosed,
      ...openingControls.map(dial => dial.close()),
      ...secondaries.map(([socket]) =>
        closeSocketWithin(socket, this.#closeGraceMs, this.#clock.scheduleTimeout),
      ),
    ]).then(() => undefined);
  }

  /** Dial one control socket for the channel. */
  async #dialControl(handlers: ControlConnectionHandlers): Promise<ControlConnection> {
    const dial = dialSocket(
      this.#path,
      this.#connectTimeoutMs,
      `Timed out connecting to server after ${this.#connectTimeoutMs}ms`,
      this.#clock.scheduleTimeout,
    );
    // Registration is synchronous with socket creation, before the first
    // await, so close() cannot miss a connection attempt already in progress.
    this.#openingControlDials.set(dial.socket, dial);
    let socket: Socket;
    try {
      socket = await dial.connected;
    } catch (error) {
      this.#openingControlDials.delete(dial.socket);
      throw error;
    }
    // Exactly one owner claims the connected socket. If close() cleared the
    // set first, it also destroyed and joined the socket, so the channel must
    // not adopt it afterward.
    if (!this.#openingControlDials.delete(socket)) {
      throw new BackendClientError('disconnected', 'Client closed during control connection');
    }
    return this.#controlConnection(socket, handlers);
  }

  /**
   * Wrap one connected socket as a control connection: a framer of its own, so
   * a superseded socket's trailing bytes can never splice into the live
   * socket's stream, and every failure routed to the channel rather than
   * thrown at whoever happened to be writing.
   */
  #controlConnection(socket: Socket, handlers: ControlConnectionHandlers): ControlConnection {
    const frames = new NewlineFramer();
    socket.setEncoding('utf8');
    socket.on('data', chunk => {
      const lines = frameChunk(frames, chunk.toString(), handlers.onFault);
      if (lines === null) return;
      for (const line of lines) {
        if (line) handlers.onFrame(line);
      }
    });
    socket.on('error', error => handlers.onDrop(transportFailure(error)));
    onPeerEnd(socket, () =>
      handlers.onDrop(new BackendClientError('disconnected', 'Server disconnected')),
    );
    return {
      send: frame =>
        socket.write(`${frame}\n`, error => {
          // A write failure means the socket is broken; report it as a drop so
          // the request is disposed by its policy and the redial loop takes over.
          if (error) handlers.onDrop(transportFailure(error));
        }),
      close: () => closeSocketWithin(socket, this.#closeGraceMs, this.#clock.scheduleTimeout),
    };
  }

  /**
   * Run one request on its own connection without a response timer.
   *
   * Chat duration is bounded by the configured agent, not by the control RPC
   * timeout. A dedicated connection also prevents a long chat from blocking
   * pause, resume, and snapshot requests in the server's per-connection loop. An
   * `AbortSignal` cancels it: the socket is torn down and the promise rejects
   * with the abort reason.
   */
  #requestLongRunning(request: IssuedRequest, signal?: AbortSignalLike): Promise<ProtocolResponse> {
    return new Promise((resolve, reject) => {
      // No already-aborted check: the channel rejects that before it calls
      // here, and this method reaches its `addEventListener` without awaiting
      // anything, so there is no window for an abort to be missed. See
      // `ControlConnector.runDedicated`.
      const socket = createConnection(this.#path);
      // A client-wide close() abandons an in-flight chat: fail it as a
      // disconnect and let close() destroy the socket.
      this.#secondarySockets.set(socket, () => {
        fail(new BackendClientError('disconnected', 'Client closed during chat'));
      });
      const frames = new NewlineFramer();
      let settled = false;
      let onAbort: (() => void) | undefined;
      let cancelConnectTimeout = (): void => {};
      cancelConnectTimeout = this.#clock.scheduleTimeout(() => {
        fail(
          new BackendClientError(
            'timeout',
            `Timed out connecting to server after ${this.#connectTimeoutMs}ms`,
          ),
        );
      }, this.#connectTimeoutMs);

      const cleanup = (): void => {
        cancelConnectTimeout();
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
        cancelConnectTimeout();
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
      onPeerEnd(socket, disconnected);
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
}

/** One socket attempt, synchronously ownable before its connection settles. */
interface SocketDial {
  readonly socket: Socket;
  readonly connected: Promise<Socket>;
  /** Cancel its deadline, reject the attempt, destroy the socket, and join it. */
  close(): Promise<void>;
}

/**
 * Start one server-socket dial and return its ownership handle immediately.
 * This is the one boundary that knows Node errnos, so everything downstream
 * branches on `kind`/`retryable`, never on the code.
 */
function dialSocket(
  path: string,
  timeoutMs: number,
  timeoutMessage: string,
  scheduleTimeout: ScheduleTimeout,
): SocketDial {
  const socket = createConnection(path);
  let cancel = (_error: BackendClientError): void => undefined;
  const connected = new Promise<Socket>((resolve, reject) => {
    let settled = false;
    let cancelTimeout = (): void => {};
    const cleanup = (): void => {
      cancelTimeout();
      socket.off('connect', onConnect);
      socket.off('error', onError);
    };
    const fail = (error: BackendClientError): void => {
      if (settled) return;
      settled = true;
      cleanup();
      reject(error);
    };
    const onError = (error: Error): void => {
      fail(dialFailure(error));
    };
    const onConnect = (): void => {
      if (settled) return;
      settled = true;
      cleanup();
      resolve(socket);
    };
    cancelTimeout = scheduleTimeout(() => {
      fail(new BackendClientError('timeout', timeoutMessage));
      socket.destroy();
    }, timeoutMs);
    cancel = fail;
    socket.once('connect', onConnect);
    socket.once('error', onError);
  });
  return {
    socket,
    connected,
    close: () => {
      cancel(new BackendClientError('disconnected', 'Client closed during control connection'));
      return destroySocket(socket);
    },
  };
}

/** Destroy a not-yet-claimed socket and join its actual close event. */
function destroySocket(socket: Socket): Promise<void> {
  return new Promise(resolve => {
    if (socket.readyState === 'closed') {
      resolve();
      return;
    }
    socket.once('close', resolve);
    socket.destroy();
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

function delay(ms: number, scheduleTimeout: ScheduleTimeout): Promise<void> {
  return new Promise(resolve => scheduleTimeout(resolve, ms));
}

function toError(error: unknown): Error {
  return error instanceof Error ? error : new Error(String(error));
}

/**
 * Report the peer ending the connection, once, however the runtime says so.
 *
 * `'end'` as well as `'close'`, because in the then-pinned Bun 1.3.9 a write
 * issued between the peer's FIN and the `'close'` that would follow it suppresses that
 * `'close'` entirely: the write neither fails nor arrives, and no further event
 * ever comes. Listening only for `'close'` therefore loses a server-initiated
 * close exactly when the client is busy, which is when it matters: the
 * connection keeps reporting itself live and every later request waits out its
 * full response deadline instead of failing as a disconnect. `'end'` arrives on
 * the FIN itself, before any write can race it, and is unambiguous for a
 * protocol that never half-closes as a normal step: a FIN means no further
 * response is coming.
 */
function onPeerEnd(socket: Socket, report: () => void): void {
  let reported = false;
  const once = (): void => {
    if (reported) return;
    reported = true;
    report();
  };
  socket.once('end', once);
  socket.once('close', once);
}

/**
 * End a socket gracefully, then force it closed if the peer does not complete
 * the FIN handshake within `graceMs`. Resolves once the socket is actually
 * closed (whether it ended or was destroyed), so a caller awaiting close cannot
 * hang on an unresponsive server.
 */
function closeSocketWithin(
  socket: Socket,
  graceMs: number,
  scheduleTimeout: ScheduleTimeout,
): Promise<void> {
  return new Promise(resolve => {
    if (socket.destroyed) return resolve();
    let cancelTimeout = (): void => {};
    cancelTimeout = scheduleTimeout(() => socket.destroy(), graceMs);
    socket.once('close', () => {
      cancelTimeout();
      resolve();
    });
    socket.end();
  });
}
