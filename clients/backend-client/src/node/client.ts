import {createConnection, type Socket} from 'node:net';
import {
  ControlChannel,
  type ControlChannelState,
  type ControlConnection,
  type ControlConnectionHandlers,
  type IssuedRequest,
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
import type {EventSubscription, SubscribeOptions} from '../transport.js';

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
}

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
  /**
   * Every secondary socket the client has opened (subscriptions and dedicated
   * requests), mapped to a hook that suppresses its own disconnect handling.
   * `close()` calls the hook before destroying, so tearing a socket down as
   * part of a client-wide close does not surface as a spurious stream
   * disconnect, whatever order the caller closes the stream and the client in.
   */
  readonly #secondarySockets = new Map<Socket, () => void>();
  readonly #channel: ControlChannel;

  private constructor(socket: Socket, path: string, options: ServerClientOptions) {
    this.#path = path;
    this.#clientId = options.clientId ?? globalThis.crypto.randomUUID();
    this.#connectTimeoutMs = options.connectTimeoutMs ?? DEFAULT_CONNECT_TIMEOUT_MS;
    this.#closeGraceMs = options.closeGraceMs ?? DEFAULT_CLOSE_GRACE_MS;
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
        client_id: this.#clientId,
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
   * Redial the control channel now, outside its backoff schedule. See
   * `ControlChannel.reconnect`.
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
   * secondary (subscription or dedicated request). Cancels a pending redial,
   * fails every request still owed an answer (in flight or held for resend),
   * and ends each socket gracefully, destroying it if it does not close within
   * the grace window, so an unresponsive server cannot hang shutdown.
   */
  close(): Promise<void> {
    // Closing the channel is the first thing that happens: it marks the client
    // closed and fails what it owes before any socket teardown can be mistaken
    // for an outage.
    const channelClosed = this.#channel.close();
    const secondaries = [...this.#secondarySockets];
    this.#secondarySockets.clear();
    for (const [, suppress] of secondaries) suppress();
    return Promise.all([
      channelClosed,
      ...secondaries.map(([socket]) => closeSocketWithin(socket, this.#closeGraceMs)),
    ]).then(() => undefined);
  }

  /** Dial one control socket for the channel. */
  async #dialControl(handlers: ControlConnectionHandlers): Promise<ControlConnection> {
    const socket = await dialSocket(
      this.#path,
      this.#connectTimeoutMs,
      `Timed out connecting to server after ${this.#connectTimeoutMs}ms`,
    );
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
    socket.on('close', () =>
      handlers.onDrop(new BackendClientError('disconnected', 'Server disconnected')),
    );
    return {
      send: frame =>
        socket.write(`${frame}\n`, error => {
          // A write failure means the socket is broken; report it as a drop so
          // the request is disposed by its policy and the redial loop takes over.
          if (error) handlers.onDrop(transportFailure(error));
        }),
      close: () => closeSocketWithin(socket, this.#closeGraceMs),
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
