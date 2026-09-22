import {randomUUID} from 'node:crypto';
import {createConnection, type Socket} from 'node:net';
import {BackendClientError, ServerError} from './errors.js';
import {NewlineFramer} from './newline-framer.js';
import type {
  Diagnostic,
  ProtocolRequest,
  ProtocolResponse,
  RequestInput,
  ServerMessage,
} from './protocol.js';

export interface EventSubscription {
  close(): Promise<void>;
}

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
}

export interface SubscribeOptions {
  /**
   * Replay at most this many of the newest events instead of the whole history.
   * A server that predates the field forbids it and rejects the subscription,
   * which is exactly how a caller probes for the capability.
   */
  tail?: number;
  /**
   * The store the caller's `afterSequence` numbers, carried by a resume so the
   * server can tell whether that cursor still belongs to the live store. Sent
   * only when non-empty; a server that predates the field forbids it and
   * rejects the subscription, so the caller falls back to a plain resume.
   */
  storeId?: string;
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

export class ServerClient {
  readonly #socket: Socket;
  readonly #path: string;
  readonly #pending = new Map<
    string,
    {
      resolve: (value: ProtocolResponse) => void;
      reject: (error: Error) => void;
      timeout: ReturnType<typeof setTimeout>;
    }
  >();
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
  readonly #responseFrames = new NewlineFramer();

  private constructor(socket: Socket, path: string, options: ServerClientOptions) {
    this.#socket = socket;
    this.#path = path;
    this.#connectTimeoutMs = options.connectTimeoutMs ?? DEFAULT_CONNECT_TIMEOUT_MS;
    this.#requestTimeoutMs = options.requestTimeoutMs ?? DEFAULT_REQUEST_TIMEOUT_MS;
    this.#closeGraceMs = options.closeGraceMs ?? DEFAULT_CLOSE_GRACE_MS;
    socket.setEncoding('utf8');
    socket.on('data', chunk => this.#onData(chunk.toString()));
    socket.on('error', error => this.#rejectAll(transportFailure(error)));
    socket.on('close', () =>
      this.#rejectAll(new BackendClientError('disconnected', 'Server disconnected')),
    );
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
    return new Promise((resolve, reject) => {
      const socket = createConnection(path);
      const onError = (error: Error): void => {
        clearTimeout(timeout);
        reject(dialFailure(error));
      };
      const timeout = setTimeout(
        () => {
          socket.destroy();
          reject(
            new BackendClientError(
              'timeout',
              `Timed out connecting to server after ${options.connectTimeoutMs ?? DEFAULT_CONNECT_TIMEOUT_MS}ms`,
            ),
          );
        },
        Math.max(0, deadline - Date.now()),
      );
      socket.once('connect', () => {
        clearTimeout(timeout);
        socket.off('error', onError);
        resolve(new ServerClient(socket, path, options));
      });
      socket.once('error', onError);
    });
  }

  request(input: RequestInput): Promise<ProtocolResponse> {
    if (this.#socket.destroyed) {
      return Promise.reject(new BackendClientError('disconnected', 'Server is disconnected'));
    }
    const requestId = randomUUID();
    const request = {
      protocol_version: 1,
      request_id: requestId,
      timestamp: new Date().toISOString(),
      ...input,
    } as ProtocolRequest;
    if (input.type === 'query.chat') return this.#requestLongRunning(request);
    return new Promise((resolve, reject) => {
      const timeout = setTimeout(() => {
        this.#pending.delete(requestId);
        reject(
          new BackendClientError(
            'timeout',
            `Server request timed out after ${this.#requestTimeoutMs}ms`,
          ),
        );
      }, this.#requestTimeoutMs);
      this.#pending.set(requestId, {resolve, reject, timeout});
      this.#socket.write(`${JSON.stringify(request)}\n`, error => {
        if (error) this.#rejectPending(requestId, transportFailure(error));
      });
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
        request_id: randomUUID(),
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
   * Close every socket the client owns: the control connection and every live
   * secondary (subscription or chat). Each is ended gracefully and destroyed if
   * it does not close within the grace window, so an unresponsive server cannot
   * hang shutdown. Suppressing each secondary's disconnect handling first makes
   * this safe whatever order the caller closes a stream and its client in.
   */
  close(graceMs: number = this.#closeGraceMs): Promise<void> {
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
   * pause, resume, and snapshot requests in the server's per-connection loop.
   */
  #requestLongRunning(request: ProtocolRequest): Promise<ProtocolResponse> {
    return new Promise((resolve, reject) => {
      const socket = createConnection(this.#path);
      // A client-wide close() abandons an in-flight chat: fail it as a
      // disconnect and let close() destroy the socket.
      this.#secondarySockets.set(socket, () => {
        fail(new BackendClientError('disconnected', 'Client closed during chat'));
      });
      const frames = new NewlineFramer();
      let settled = false;
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
    });
  }

  #onData(chunk: string): void {
    // An unframable stream (an oversized newline-less remainder) cannot be read
    // further; fail every pending request and drop the connection.
    const lines = frameChunk(this.#responseFrames, chunk, error => {
      this.#rejectAll(error);
      this.#socket.destroy();
    });
    if (lines === null) return;
    for (const line of lines) {
      if (!line) continue;
      let response: ProtocolResponse;
      try {
        response = parseProtocolResponse(line);
      } catch (error) {
        this.#rejectAll(streamFailure(error));
        this.#socket.destroy();
        return;
      }
      const pending = this.#pending.get(response.request_id);
      if (!pending) continue;
      this.#pending.delete(response.request_id);
      clearTimeout(pending.timeout);
      if (response.ok) pending.resolve(response);
      else pending.reject(responseError(response));
    }
  }

  #rejectAll(error: Error): void {
    for (const pending of this.#pending.values()) {
      clearTimeout(pending.timeout);
      pending.reject(error);
    }
    this.#pending.clear();
  }

  #rejectPending(requestId: string, error: Error): void {
    const pending = this.#pending.get(requestId);
    if (pending === undefined) return;
    this.#pending.delete(requestId);
    clearTimeout(pending.timeout);
    pending.reject(error);
  }
}

/**
 * Classify one dial attempt's failure, at the one boundary that knows Node
 * errnos: only the errors a still-starting server produces are transient.
 * Everything downstream branches on `kind` and `retryable`, never on the code.
 */
function dialFailure(error: Error): BackendClientError {
  const code = (error as NodeJS.ErrnoException).code;
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
