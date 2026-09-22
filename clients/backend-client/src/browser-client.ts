import type {ProtocolRequest, ProtocolResponse, RequestInput, ServerMessage} from './protocol.js';
import {
  type EventSubscription,
  parseProtocolResponse,
  parseServerMessage,
  responseError,
  ServerError,
  type SubscribeOptions,
} from './transport.js';

export interface BrowserBackendClientOptions {
  connectTimeoutMs?: number;
  requestTimeoutMs?: number;
}

/** HTTP requests and independent WebSocket subscriptions to the browser gateway. */
export class BrowserBackendClient {
  readonly #requestUrl: URL;
  readonly #eventsUrl: URL;
  readonly #connectTimeoutMs: number;
  readonly #requestTimeoutMs: number;
  readonly #requests = new Set<AbortController>();
  readonly #subscriptions = new Set<() => Promise<void>>();
  #closed = false;

  constructor(baseUrl = window.location.origin, options: BrowserBackendClientOptions = {}) {
    this.#requestUrl = new URL('/api/request', baseUrl);
    if (!['http:', 'https:'].includes(this.#requestUrl.protocol)) {
      throw new Error('Browser backend URL must use http or https');
    }
    this.#eventsUrl = new URL('/api/events', this.#requestUrl);
    this.#eventsUrl.protocol = this.#requestUrl.protocol === 'https:' ? 'wss:' : 'ws:';
    this.#connectTimeoutMs = options.connectTimeoutMs ?? 5_000;
    this.#requestTimeoutMs = options.requestTimeoutMs ?? 30_000;
  }

  async request(input: RequestInput): Promise<ProtocolResponse> {
    if (this.#closed) throw new Error('Server client is closed');
    const request: ProtocolRequest = {
      ...input,
      protocol_version: 1,
      request_id: crypto.randomUUID(),
      timestamp: new Date().toISOString(),
    };
    const controller = new AbortController();
    this.#requests.add(controller);
    // Agent-backed chat has no response deadline, matching ServerClient.
    const timeout =
      input.type === 'query.chat'
        ? undefined
        : setTimeout(() => {
            controller.abort(
              new Error(`Server request timed out after ${this.#requestTimeoutMs}ms`),
            );
          }, this.#requestTimeoutMs);
    try {
      const http = await fetch(this.#requestUrl, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(request),
        credentials: 'same-origin',
        redirect: 'error',
        signal: controller.signal,
      });
      const body = await http.text();
      const httpError = () =>
        new Error(`Server HTTP request failed: ${http.status} ${http.statusText}`);
      let response: ProtocolResponse;
      try {
        response = parseProtocolResponse(body);
      } catch (error) {
        if (!http.ok) throw httpError();
        throw error;
      }
      if (response.request_id !== request.request_id) {
        throw new Error('Server response has an unexpected request ID');
      }
      if (!response.ok) throw responseError(response);
      if (!http.ok) throw httpError();
      return response;
    } catch (error) {
      if (controller.signal.aborted) throw controller.signal.reason;
      throw error;
    } finally {
      clearTimeout(timeout);
      this.#requests.delete(controller);
    }
  }

  subscribe(
    afterSequence: number,
    onMessage: (message: ServerMessage) => void,
    onDisconnect: (error: Error) => void,
    options: SubscribeOptions = {},
  ): Promise<EventSubscription> {
    if (this.#closed) return Promise.reject(new Error('Server client is closed'));
    return new Promise((resolve, reject) => {
      const request: ProtocolRequest = {
        protocol_version: 1,
        request_id: crypto.randomUUID(),
        timestamp: new Date().toISOString(),
        type: 'subscribe',
        after_sequence: afterSequence,
        ...(options.tail === undefined ? {} : {tail: options.tail}),
      };
      const socket = new WebSocket(this.#eventsUrl);
      let subscribed = false;
      let stopped = false;
      const closed = new Promise<void>(resolveClosed => {
        socket.addEventListener('close', () => resolveClosed(), {once: true});
      });
      const stop = (): Promise<void> => {
        if (!stopped) {
          stopped = true;
          clearTimeout(handshakeTimeout);
          this.#subscriptions.delete(close);
          socket.onopen = null;
          socket.onmessage = null;
          socket.onerror = null;
          socket.onclose = null;
          socket.close();
        }
        return closed;
      };
      const close = (): Promise<void> => {
        if (!subscribed && !stopped) reject(new Error('Server client is closed'));
        return stop();
      };
      const disconnect = (error: Error): void => {
        if (stopped) return;
        void stop();
        if (subscribed) onDisconnect(error);
        else reject(error);
      };
      const handshakeTimeout = setTimeout(() => {
        disconnect(new Error(`Server subscription timed out after ${this.#connectTimeoutMs}ms`));
      }, this.#connectTimeoutMs);
      this.#subscriptions.add(close);
      socket.onopen = () => {
        try {
          socket.send(JSON.stringify(request));
        } catch (error) {
          disconnect(error instanceof Error ? error : new Error(String(error)));
        }
      };
      socket.onmessage = event => {
        if (stopped) return;
        try {
          if (typeof event.data !== 'string') {
            throw new Error('Invalid server event-stream message: expected JSON text');
          }
          const message = parseServerMessage(event.data);
          if (message.type === 'subscribed' && message.request_id !== request.request_id) {
            throw new Error('Server subscription has an unexpected request ID');
          }
          onMessage(message);
          if (stopped) return;
          if (message.type === 'protocol_error') {
            void stop();
            if (!subscribed) reject(new ServerError(message.message, message.diagnostic ?? null));
          } else if (!subscribed && message.type === 'subscribed') {
            subscribed = true;
            clearTimeout(handshakeTimeout);
            resolve({close});
          }
        } catch (error) {
          disconnect(error instanceof Error ? error : new Error(String(error)));
        }
      };
      socket.onerror = () => disconnect(new Error('Server event stream connection failed'));
      socket.onclose = () =>
        disconnect(
          new Error(
            subscribed
              ? 'Server event stream disconnected'
              : 'Server event stream disconnected before subscription',
          ),
        );
    });
  }

  /** Abort requests and close subscriptions without terminating the backend run. */
  async close(): Promise<void> {
    this.#closed = true;
    for (const controller of this.#requests) controller.abort(new Error('Server client is closed'));
    await Promise.all([...this.#subscriptions].map(close => close()));
  }
}
