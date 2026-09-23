import type {ServerMessage, ServerTransport} from '@vibesys/backend-client';
import {hasRunEnded} from '@vibesys/core-state';
import {
  PersistentEventStream,
  type PersistentEventStreamOptions,
  type StreamConnectionState,
  WebSocketTransport,
} from './browser-entry.js';
import {type CoreStateStore, createCoreStateStore} from './store.js';

export type WebSessionStatus = 'connecting' | 'connected' | 'stale';

export interface BrowserLifecycle {
  readonly visibilityState: DocumentVisibilityState;
  readonly online: boolean;
  addEventListener(type: 'visibilitychange' | 'online' | 'offline', listener: () => void): void;
  removeEventListener(type: 'visibilitychange' | 'online' | 'offline', listener: () => void): void;
}

export interface WebSessionState {
  readonly status: WebSessionStatus;
  readonly error: Error | null;
}

export interface WebSessionOptions {
  readonly lifecycle?: BrowserLifecycle;
  readonly transport?: ServerTransport;
  readonly reconnectDelaysMs?: readonly number[];
  readonly tail?: number;
}

/**
 * Browser-only lifecycle policy around the transport stream. It wakes the
 * finite reconnect schedule on visibility and network transitions, while the
 * transport and core-state layers remain unaware of browser lifecycle APIs.
 */
export class WebSession {
  readonly store: CoreStateStore;
  readonly #transport: ServerTransport;
  readonly #stream: PersistentEventStream;
  readonly #lifecycle: BrowserLifecycle;
  readonly #listeners = new Set<() => void>();

  #status: WebSessionStatus = 'connecting';
  #error: Error | null = null;
  #state: WebSessionState = {status: 'connecting', error: null};
  #storeId = '';
  #started = false;
  #closed = false;
  #wakeInFlight: Promise<void> | null = null;

  constructor(options: WebSessionOptions = {}) {
    this.store = createCoreStateStore();
    this.#transport =
      options.transport ?? new WebSocketTransport(webSocketUrlFromLocation(window.location));
    const streamOptions: PersistentEventStreamOptions = {};
    if (options.tail !== undefined) streamOptions.tail = options.tail;
    if (options.reconnectDelaysMs !== undefined) {
      streamOptions.reconnectDelaysMs = options.reconnectDelaysMs;
    }
    this.#stream = new PersistentEventStream(this.#transport, streamOptions);
    this.#lifecycle = options.lifecycle ?? browserLifecycle;
    this.#lifecycle.addEventListener('visibilitychange', this.#wake);
    this.#lifecycle.addEventListener('online', this.#wake);
    this.#lifecycle.addEventListener('offline', this.#offline);
  }

  getState = (): WebSessionState => this.#state;

  subscribe = (listener: () => void): (() => void) => {
    this.#listeners.add(listener);
    return () => this.#listeners.delete(listener);
  };

  async start(): Promise<void> {
    if (this.#started) return;
    this.#started = true;
    let snapshotError: Error | null = null;
    try {
      await this.#loadSnapshot();
    } catch (error) {
      snapshotError = toError(error);
      this.#setState('stale', snapshotError);
    }
    try {
      await this.#stream.subscribe({
        cursor: () => this.store.getState().sequence,
        storeId: () => this.#storeId,
        shouldReconnect: () => !hasRunEnded(this.store.getState()),
        onMessage: this.#onMessage,
        onConnectionState: this.#onConnectionState,
      });
      if (this.#status === 'connecting' && snapshotError === null) {
        this.#setState('connected', null);
      }
    } catch (error) {
      this.#setState('stale', toError(error));
    }
  }

  async close(): Promise<void> {
    if (this.#closed) return;
    this.#closed = true;
    this.#lifecycle.removeEventListener('visibilitychange', this.#wake);
    this.#lifecycle.removeEventListener('online', this.#wake);
    this.#lifecycle.removeEventListener('offline', this.#offline);
    await this.#stream.close();
    await this.#transport.close();
  }

  /** One-click recovery after a visible stale state. */
  reattach(): void {
    if (this.#closed || hasRunEnded(this.store.getState())) return;
    void this.#wake();
  }

  #loadSnapshot = async (): Promise<void> => {
    const response = await this.#transport.request({type: 'query.snapshot'});
    if (response.ok !== true || response.snapshot === null || response.snapshot === undefined) {
      throw new Error(response.error ?? 'Server did not return a run snapshot');
    }
    this.store.applySnapshot(response.snapshot);
  };

  #onMessage = (message: ServerMessage): void => {
    if (message.type !== 'event_batch') return;
    const messageStoreId = message.store_id ?? '';
    const replaced =
      this.#storeId !== '' && messageStoreId !== '' && messageStoreId !== this.#storeId;
    if (messageStoreId !== '') this.#storeId = messageStoreId;
    this.store.applyBatch(message, replaced);
  };

  #onConnectionState = (state: StreamConnectionState): void => {
    if (state.status === 'connected') this.#setState('connected', null);
    else if (!hasRunEnded(this.store.getState())) this.#setState('stale', state.error);
  };

  #wake = (): void => {
    if (this.#closed || this.#lifecycle.visibilityState === 'hidden' || !this.#lifecycle.online) {
      return;
    }
    if (this.#wakeInFlight !== null) return;
    this.#wakeInFlight = this.#resume().finally(() => {
      this.#wakeInFlight = null;
    });
  };

  #offline = (): void => {
    if (!this.#closed && !hasRunEnded(this.store.getState())) {
      this.#setState('stale', new Error('Network is offline'));
    }
  };

  async #resume(): Promise<void> {
    try {
      await this.#loadSnapshot();
      this.#stream.retry();
    } catch (error) {
      this.#setState('stale', toError(error));
      this.#stream.retry();
    }
  }

  #setState(status: WebSessionStatus, error: Error | null): void {
    if (this.#status === status && this.#error === error) return;
    this.#status = status;
    this.#error = error;
    this.#state = {status, error};
    for (const listener of this.#listeners) listener();
  }
}

export function webSocketUrlFromLocation(location: Location): string {
  const page = new URL(location.href);
  const gateway = page.searchParams.get('gateway');
  const url = gateway === null ? page : new URL(gateway, page.origin);
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  url.pathname = '/ws';
  const token = url.searchParams.get('token') ?? page.searchParams.get('token') ?? '';
  url.search = new URLSearchParams({token}).toString();
  return url.toString();
}

function toError(error: unknown): Error {
  return error instanceof Error ? error : new Error(String(error));
}

const browserLifecycle: BrowserLifecycle = {
  get visibilityState() {
    return document.visibilityState;
  },
  get online() {
    return navigator.onLine;
  },
  addEventListener(type, listener) {
    window.addEventListener(type, listener);
  },
  removeEventListener(type, listener) {
    window.removeEventListener(type, listener);
  },
};
