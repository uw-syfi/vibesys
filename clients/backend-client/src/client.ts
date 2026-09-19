import {randomUUID} from 'node:crypto';
import {createConnection, type Socket} from 'node:net';
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
/** Errors a not-yet-listening server produces; anything else is fatal. */
const RETRYABLE_CONNECT_CODES = new Set(['ENOENT', 'ECONNREFUSED']);

/** A failed server response, including its optional structured diagnostic. */
export class ServerError extends Error {
  constructor(
    message: string,
    readonly diagnostic: Diagnostic | null = null,
  ) {
    super(message);
    this.name = 'ServerError';
  }
}

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
  readonly #longRunningSockets = new Set<Socket>();
  readonly #responseFrames = new NewlineFramer();

  private constructor(socket: Socket, path: string, options: ServerClientOptions) {
    this.#socket = socket;
    this.#path = path;
    this.#connectTimeoutMs = options.connectTimeoutMs ?? DEFAULT_CONNECT_TIMEOUT_MS;
    this.#requestTimeoutMs = options.requestTimeoutMs ?? DEFAULT_REQUEST_TIMEOUT_MS;
    socket.setEncoding('utf8');
    socket.on('data', chunk => this.#onData(chunk.toString()));
    socket.on('error', error => this.#rejectAll(error));
    socket.on('close', () => this.#rejectAll(new Error('Server disconnected')));
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
        if (!isRetryableConnectError(error)) throw error;
        lastError = error;
      }
      if (Date.now() + retryIntervalMs >= deadline) {
        throw new Error(
          `Timed out connecting to server after ${timeoutMs}ms: ${lastError?.message}`,
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
        reject(error);
      };
      const timeout = setTimeout(
        () => {
          socket.destroy();
          reject(
            new Error(
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
      return Promise.reject(new Error('Server is disconnected'));
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
        reject(new Error(`Server request timed out after ${this.#requestTimeoutMs}ms`));
      }, this.#requestTimeoutMs);
      this.#pending.set(requestId, {resolve, reject, timeout});
      this.#socket.write(`${JSON.stringify(request)}\n`, error => {
        if (error) this.#rejectPending(requestId, error);
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
        disconnect(new Error(`Server subscription timed out after ${this.#connectTimeoutMs}ms`));
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
      const handleSubscriptionLine = (line: string): boolean => {
        if (!line) return true;
        let message: ServerMessage;
        try {
          message = parseServerMessage(line);
          onMessage(message);
        } catch (error) {
          disconnect(toError(error));
          socket.destroy();
          return false;
        }
        if (message.type === 'protocol_error') protocolErrorReceived = true;
        if (!subscribed && message.type === 'subscribed') {
          subscribed = true;
          clearTimeout(handshakeTimeout);
          resolve({
            close: () => {
              closing = true;
              clearTimeout(handshakeTimeout);
              return closeSocket(socket);
            },
          });
        }
        return true;
      };
      socket.setEncoding('utf8');
      socket.once('connect', () => {
        socket.write(
          `${JSON.stringify({
            protocol_version: 1,
            request_id: randomUUID(),
            timestamp: new Date().toISOString(),
            type: 'subscribe',
            after_sequence: afterSequence,
            // Omitted rather than sent as null: an old server forbids unknown
            // fields, so a default subscribe must stay byte-for-byte what it
            // has always been.
            ...(options.tail === undefined ? {} : {tail: options.tail}),
            ...(options.storeId ? {store_id: options.storeId} : {}),
          })}\n`,
        );
      });
      socket.on('data', chunk => {
        for (const line of frames.push(chunk.toString())) {
          if (!handleSubscriptionLine(line)) return;
        }
      });
      socket.once('error', disconnect);
      socket.once('close', () => {
        disconnect(
          new Error(
            subscribed
              ? 'Server event stream disconnected'
              : 'Server event stream disconnected before subscription',
          ),
        );
      });
    });
  }

  close(): Promise<void> {
    for (const socket of this.#longRunningSockets) socket.destroy();
    this.#longRunningSockets.clear();
    return new Promise(resolve => {
      if (this.#socket.destroyed) return resolve();
      this.#socket.once('close', resolve);
      this.#socket.end();
    });
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
      this.#longRunningSockets.add(socket);
      const frames = new NewlineFramer();
      let settled = false;
      const connectTimeout = setTimeout(() => {
        fail(new Error(`Timed out connecting to server after ${this.#connectTimeoutMs}ms`));
      }, this.#connectTimeoutMs);

      const cleanup = (): void => {
        clearTimeout(connectTimeout);
        this.#longRunningSockets.delete(socket);
        socket.off('error', fail);
        socket.off('close', disconnected);
      };
      const fail = (error: Error): void => {
        if (settled) return;
        settled = true;
        cleanup();
        socket.destroy();
        reject(error);
      };
      const disconnected = (): void => fail(new Error('Server disconnected during chat'));
      const finish = (response: ProtocolResponse): void => {
        if (settled) return;
        if (response.request_id !== request.request_id) {
          fail(new Error('Server chat response has an unexpected request ID'));
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
        for (const line of frames.push(chunk.toString())) {
          if (handleChatResponseLine(line)) return;
        }
      });
      socket.once('error', fail);
      socket.once('close', disconnected);
    });
  }

  #onData(chunk: string): void {
    for (const line of this.#responseFrames.push(chunk)) {
      if (!line) continue;
      let response: ProtocolResponse;
      try {
        response = parseProtocolResponse(line);
      } catch (error) {
        const parseError = error instanceof Error ? error : new Error(String(error));
        this.#rejectAll(parseError);
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

function isRetryableConnectError(error: unknown): error is Error {
  if (!(error instanceof Error)) return false;
  const code = (error as NodeJS.ErrnoException).code;
  return code !== undefined && RETRYABLE_CONNECT_CODES.has(code);
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

function closeSocket(socket: Socket): Promise<void> {
  return new Promise(resolve => {
    if (socket.destroyed) return resolve();
    socket.once('close', resolve);
    socket.end();
  });
}

function parseProtocolResponse(line: string): ProtocolResponse {
  const value = parseRecord(line, 'response');
  if (value['protocol_version'] !== 1) throw new Error('Unsupported server protocol version');
  if (typeof value['request_id'] !== 'string') {
    throw new Error('Invalid server response: request_id must be a string');
  }
  if (typeof value['ok'] !== 'boolean') {
    throw new Error('Invalid server response: ok must be a boolean');
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
      throw new Error('Invalid subscribed message');
    }
  } else if (type === 'event') {
    if (!isRecord(value['event'])) throw new Error('Invalid event message');
  } else if (type === 'event_batch') {
    if (!Array.isArray(value['events'])) throw new Error('Invalid event batch message');
  } else if (type === 'protocol_error') {
    if (typeof value['code'] !== 'string' || typeof value['message'] !== 'string') {
      throw new Error('Invalid protocol error message');
    }
  } else {
    throw new Error(`Unknown server event-stream message: ${String(type)}`);
  }
  return value as unknown as ServerMessage;
}

function parseRecord(line: string, description: string): Record<string, unknown> {
  let value: unknown;
  try {
    value = JSON.parse(line);
  } catch (error) {
    throw new Error(
      `Invalid server ${description} JSON: ${error instanceof Error ? error.message : String(error)}`,
    );
  }
  if (!isRecord(value)) throw new Error(`Invalid server ${description}: expected an object`);
  return value;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}
