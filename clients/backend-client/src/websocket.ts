import {BackendClientError, ServerError} from './errors.js';
import type {ProtocolRequest, ProtocolResponse, RequestInput, ServerMessage} from './protocol.js';
import {
  parseProtocolResponse,
  parseServerMessage,
  responseError,
  streamFailure,
} from './protocol-parse.js';
import type {EventSubscription, ServerTransport, SubscribeOptions} from './transport.js';

const OPEN = 1;
const DEFAULT_CONNECT_TIMEOUT_MS = 5_000;
const DEFAULT_REQUEST_TIMEOUT_MS = 30_000;
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
  /** Override the browser constructor in tests or an embedded web runtime. */
  webSocket?: (url: string) => WebSocketLike;
  /** Override timeout scheduling in deterministic tests. */
  scheduleTimeout?: (callback: () => void, delayMs: number) => () => void;
}

type IssuedRequest = ProtocolRequest & {readonly request_id: string};

/**
 * Browser transport for the protocol's three connection roles. WebSocket
 * frames are already message-delimited, so no Node stream or newline framer
 * reaches this entry point. The control socket is intentionally not retried
 * here; `PersistentEventStream` owns event-stream reconnect policy, while a
 * caller can recreate this transport after a gateway outage.
 */
export class WebSocketTransport implements ServerTransport {
  readonly #url: string;
  readonly #clientId: string;
  readonly #connectTimeoutMs: number;
  readonly #requestTimeoutMs: number;
  readonly #closeGraceMs: number;
  readonly #webSocket: (url: string) => WebSocketLike;
  readonly #pending = new Map<
    string,
    {
      readonly resolve: (response: ProtocolResponse) => void;
      readonly reject: (error: Error) => void;
      readonly cancelTimeout: () => void;
    }
  >();
  readonly #secondarySockets = new Map<WebSocketLike, () => void>();
  readonly #openingSockets = new Set<WebSocketLike>();
  readonly #scheduleTimeout: (callback: () => void, delayMs: number) => () => void;

  #control: WebSocketLike | null = null;
  #controlDial: Promise<WebSocketLike> | null = null;
  #closed = false;

  constructor(url: string, options: WebSocketTransportOptions = {}) {
    this.#url = url;
    this.#clientId = options.clientId ?? globalThis.crypto.randomUUID();
    this.#connectTimeoutMs = options.connectTimeoutMs ?? DEFAULT_CONNECT_TIMEOUT_MS;
    this.#requestTimeoutMs = options.requestTimeoutMs ?? DEFAULT_REQUEST_TIMEOUT_MS;
    this.#closeGraceMs = options.closeGraceMs ?? DEFAULT_CLOSE_GRACE_MS;
    this.#webSocket = options.webSocket ?? defaultWebSocket;
    this.#scheduleTimeout = options.scheduleTimeout ?? scheduleTimeout;
  }

  async request(input: RequestInput): Promise<ProtocolResponse> {
    if (this.#closed) throw disconnectedError('Client is closed');
    const request = makeRequest(input, this.#clientId);
    if (input.type === 'query.chat') return this.#requestChat(request);
    const socket = await this.#ensureControl();
    return new Promise((resolve, reject) => {
      const cancelTimeout = this.#scheduleTimeout(() => {
        this.#pending.delete(request.request_id);
        reject(
          new BackendClientError(
            'timeout',
            `Server request timed out after ${this.#requestTimeoutMs}ms`,
          ),
        );
      }, this.#requestTimeoutMs);
      this.#pending.set(request.request_id, {resolve, reject, cancelTimeout});
      try {
        socket.send(JSON.stringify(request));
      } catch (error) {
        this.#dropControl(transportFailure(error));
      }
    });
  }

  async subscribe(
    afterSequence: number,
    onMessage: (message: ServerMessage) => void,
    onDisconnect: (error: Error) => void,
    options: SubscribeOptions = {},
  ): Promise<EventSubscription> {
    if (this.#closed) throw disconnectedError('Client is closed');
    const socket = await this.#openSocket();
    if (this.#closed) {
      await closeSocket(socket, this.#closeGraceMs, this.#scheduleTimeout);
      throw disconnectedError('Client is closed');
    }
    const request = makeRequest(
      {
        type: 'subscribe',
        after_sequence: afterSequence,
        ...(options.tail === undefined ? {} : {tail: options.tail}),
        ...(options.storeId === undefined ? {} : {store_id: options.storeId}),
      } as RequestInput,
      this.#clientId,
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
        this.#secondarySockets.delete(socket);
        if (subscribed && !protocolErrorReceived) onDisconnect(error);
        else if (!subscribed) reject(error);
      };
      this.#secondarySockets.set(socket, () => {
        cancelHandshake();
        if (subscribed) {
          closing = true;
          return;
        }
        if (disconnected) return;
        disconnected = true;
        reject(disconnectedError('Client closed during subscription'));
      });
      cancelHandshake = this.#scheduleTimeout(() => {
        disconnect(
          new BackendClientError(
            'timeout',
            `Server subscription timed out after ${this.#connectTimeoutMs}ms`,
          ),
        );
        void closeSocket(socket, this.#closeGraceMs, this.#scheduleTimeout);
      }, this.#connectTimeoutMs);
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
              void closeSocket(socket, this.#closeGraceMs, this.#scheduleTimeout);
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
                this.#secondarySockets.delete(socket);
                await closeSocket(socket, this.#closeGraceMs, this.#scheduleTimeout);
              },
            });
          }
        } catch (error) {
          const failure = streamFailure(error);
          disconnect(failure);
          void closeSocket(socket, this.#closeGraceMs, this.#scheduleTimeout);
        }
      };
      socket.onerror = () => {
        disconnect(transportFailure(new Error('WebSocket transport error')));
        void closeSocket(socket, this.#closeGraceMs, this.#scheduleTimeout);
      };
      socket.onclose = () => {
        this.#secondarySockets.delete(socket);
        disconnect(
          disconnectedError(
            subscribed ? 'Server event stream disconnected' : 'Server rejected the subscription',
          ),
        );
      };
      try {
        socket.send(JSON.stringify(request));
      } catch (error) {
        disconnect(transportFailure(error));
        void closeSocket(socket, this.#closeGraceMs, this.#scheduleTimeout);
      }
    });
  }

  async close(): Promise<void> {
    this.#closed = true;
    const pending = [...this.#pending.values()];
    this.#pending.clear();
    for (const entry of pending) {
      entry.cancelTimeout();
      entry.reject(disconnectedError('Client closed'));
    }
    const secondaries = [...this.#secondarySockets.entries()];
    this.#secondarySockets.clear();
    for (const [, settle] of secondaries) settle();
    const sockets = new Set([
      ...this.#openingSockets,
      ...secondaries.map(([socket]) => socket),
      ...(this.#control === null ? [] : [this.#control]),
    ]);
    this.#openingSockets.clear();
    this.#control = null;
    await Promise.all(
      [...sockets].map(socket => closeSocket(socket, this.#closeGraceMs, this.#scheduleTimeout)),
    );
  }

  async #ensureControl(): Promise<WebSocketLike> {
    if (this.#control?.readyState === OPEN) return this.#control;
    this.#controlDial ??= this.#openSocket()
      .then(socket => {
        if (this.#closed) {
          void closeSocket(socket, this.#closeGraceMs, this.#scheduleTimeout);
          throw disconnectedError('Client is closed');
        }
        this.#control = socket;
        socket.onmessage = event => this.#onControlMessage(event.data);
        socket.onerror = () =>
          this.#dropControl(transportFailure(new Error('WebSocket transport error')));
        socket.onclose = () => this.#dropControl(disconnectedError('Server disconnected'));
        return socket;
      })
      .finally(() => {
        this.#controlDial = null;
      });
    return this.#controlDial;
  }

  async #openSocket(): Promise<WebSocketLike> {
    let socket: WebSocketLike;
    try {
      socket = this.#webSocket(this.#url);
    } catch (error) {
      throw transportFailure(error);
    }
    this.#openingSockets.add(socket);
    if (socket.readyState === OPEN) {
      this.#openingSockets.delete(socket);
      return socket;
    }
    return new Promise((resolve, reject) => {
      let settled = false;
      const cancelTimeout = this.#scheduleTimeout(() => {
        if (settled) return;
        settled = true;
        this.#openingSockets.delete(socket);
        void closeSocket(socket, this.#closeGraceMs, this.#scheduleTimeout);
        reject(
          new BackendClientError(
            'timeout',
            `Timed out connecting to server after ${this.#connectTimeoutMs}ms`,
          ),
        );
      }, this.#connectTimeoutMs);
      socket.onopen = () => {
        if (settled) return;
        settled = true;
        cancelTimeout();
        this.#openingSockets.delete(socket);
        resolve(socket);
      };
      socket.onerror = () => {
        if (settled) return;
        settled = true;
        cancelTimeout();
        this.#openingSockets.delete(socket);
        void closeSocket(socket, this.#closeGraceMs, this.#scheduleTimeout);
        reject(transportFailure(new Error('WebSocket transport error')));
      };
      socket.onclose = () => {
        if (settled) return;
        settled = true;
        cancelTimeout();
        this.#openingSockets.delete(socket);
        reject(disconnectedError('Server disconnected before WebSocket opened'));
      };
    });
  }

  #onControlMessage(data: unknown): void {
    try {
      const response = parseProtocolResponse(textFrame(data));
      const pending = this.#pending.get(response.request_id);
      if (pending === undefined) return;
      this.#pending.delete(response.request_id);
      pending.cancelTimeout();
      if (response.ok) pending.resolve(response);
      else pending.reject(responseError(response));
    } catch (error) {
      this.#dropControl(streamFailure(error));
    }
  }

  #dropControl(error: Error): void {
    if (this.#control === null) return;
    const socket = this.#control;
    this.#control = null;
    for (const [requestId, pending] of this.#pending) {
      pending.cancelTimeout();
      pending.reject(error);
      this.#pending.delete(requestId);
    }
    socket.onmessage = null;
    void closeSocket(socket, this.#closeGraceMs, this.#scheduleTimeout);
  }

  async #requestChat(request: IssuedRequest): Promise<ProtocolResponse> {
    const socket = await this.#openSocket();
    if (this.#closed) {
      await closeSocket(socket, this.#closeGraceMs, this.#scheduleTimeout);
      throw disconnectedError('Client is closed');
    }
    return new Promise((resolve, reject) => {
      let settled = false;
      const finish = (callback: () => void): void => {
        if (settled) return;
        settled = true;
        this.#secondarySockets.delete(socket);
        callback();
        void closeSocket(socket, this.#closeGraceMs, this.#scheduleTimeout);
      };
      this.#secondarySockets.set(socket, () => {
        if (settled) return;
        settled = true;
        reject(disconnectedError('Client closed during chat'));
      });
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
}

function makeRequest(input: RequestInput, clientId: string): IssuedRequest {
  return {
    protocol_version: 1,
    request_id: globalThis.crypto.randomUUID(),
    client_id: clientId,
    timestamp: new Date().toISOString(),
    ...input,
  } as IssuedRequest;
}

function defaultWebSocket(url: string): WebSocketLike {
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

function scheduleTimeout(callback: () => void, delayMs: number): () => void {
  const timer = setTimeout(callback, delayMs);
  return () => clearTimeout(timer);
}

function closeSocket(
  socket: WebSocketLike,
  graceMs: number,
  schedule: (callback: () => void, delayMs: number) => () => void,
): Promise<void> {
  return new Promise(resolve => {
    if (socket.readyState === 3) {
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
