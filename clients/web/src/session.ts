import {
  type ControlChannelState,
  type ControlTransport,
  type ServerMessage,
  sameControlChannelState,
} from '@vibesys/backend-client';
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
  /** The event stream: whether the transcript on screen is still growing. */
  readonly status: WebSessionStatus;
  readonly error: Error | null;
  /**
   * Whether a control request issued now can be delivered, as the transport's
   * control channel reports it. A different fact from a stale transcript: the
   * stream and the command path fail independently, and only this one says the
   * request/response half is inert. Today that costs the page its
   * `query.snapshot`, so `reattach()` cannot reload the run's state; the run
   * controls that #815 adds ride the same channel.
   *
   * `connected` until the channel reports otherwise, because there is nothing
   * to report before the first dial settles. That is a starting value, not a
   * claim: the page issues `query.snapshot` from `start()`, so the first dial
   * is already under way by the time anything renders, and a cold-start failure
   * arrives as a `disconnected` report like any other.
   *
   * An outage is reported whenever the channel reports one, including on a run
   * that has already ended. Whether it is worth showing then is the frontend's
   * decision, and `connectionBanners` in `banners.ts` makes it.
   */
  readonly controls: ControlChannelState;
}

/** What the session wires into the transport it drives. */
export interface WebSessionTransportHooks {
  /** Where the transport reports control-channel connectivity. */
  readonly onConnectionState: (state: ControlChannelState) => void;
}

export interface WebSessionOptions {
  readonly lifecycle?: BrowserLifecycle;
  /**
   * Build the transport this session drives, wired to the session's observers.
   * Defaults to a `WebSocketTransport` on the page's gateway URL. A factory
   * rather than an instance because the session, not the caller, owns what the
   * transport reports to: an already-constructed transport could not be told
   * where to report a dead control channel after the fact.
   */
  readonly transport?: (hooks: WebSessionTransportHooks) => ControlTransport;
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
  readonly #transport: ControlTransport;
  readonly #stream: PersistentEventStream;
  readonly #lifecycle: BrowserLifecycle;
  readonly #listeners = new Set<() => void>();

  #status: WebSessionStatus = 'connecting';
  #error: Error | null = null;
  #controls: ControlChannelState = {status: 'connected'};
  #state: WebSessionState = {status: 'connecting', error: null, controls: {status: 'connected'}};
  #storeId = '';
  #started = false;
  #closed = false;
  #wakeInFlight: Promise<void> | null = null;

  constructor(options: WebSessionOptions = {}) {
    this.store = createCoreStateStore();
    const hooks: WebSessionTransportHooks = {
      onConnectionState: state => this.#onControlState(state),
    };
    this.#transport =
      options.transport === undefined
        ? this.#browserTransport(hooks, options.reconnectDelaysMs)
        : options.transport(hooks);
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

  /**
   * Redial the control channel now. This is what the controls-unavailable
   * affordance calls, and it is deliberately not `reattach()`: a channel that
   * reported a drop has a redial already armed, so issuing another request only
   * queues behind that backoff and a click would change nothing observable for
   * as long as the schedule says. `ControlTransport.reconnect` cancels the armed
   * redial and dials, which is the only thing a user asking to reconnect can
   * mean. It touches the event stream not at all: a dead command path with a
   * live transcript is exactly the case this exists for.
   *
   * No `hasRunEnded` guard, unlike `reattach()`, because this does not decide
   * when it is offered: `connectionBanners` does, and it withholds the banner
   * on an ended run. The verb stays unconditional so the one place that judges
   * an ended run is the one place that renders the affordance.
   */
  reconnectControls(): void {
    if (this.#closed) return;
    this.#transport.reconnect();
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

  /**
   * The event stream changed state. Every drop it reports is published,
   * including one on a run that has already ended, for the same reason
   * `#onControlState` publishes a control outage then: the session reports what
   * the transport did and `connectionBanners` decides what is worth showing.
   *
   * Judging it here is what made an ended run render blank. The stream stops
   * at whatever it had folded, which for a run reopened after it finished is
   * nothing at all, and suppressing the report left the page with a terminal
   * status chip over an empty transcript and no statement that it was short
   * (#1044). Whether the run can be resubscribed is a separate question, and
   * `ConnectionBanners.stream.reattach` is where it is answered.
   */
  #onConnectionState = (state: StreamConnectionState): void => {
    if (state.status === 'connected') this.#setState('connected', null);
    else this.#setState('stale', state.error);
  };

  /**
   * The control channel changed state. Kept separate from the stream's status:
   * a dead command path with a live transcript, and a stale transcript with
   * deliverable commands, are both real and a frontend acts on them
   * differently.
   *
   * Every outage the channel reports is published, including one on a run that
   * has already ended. Whether it is worth putting on screen is the frontend's
   * decision, and `connectionBanners` in `banners.ts` makes it. Judging it here
   * instead latches: the control channel and the event stream are two
   * independent sockets with no ordering guarantee, so a drop can be reported
   * while the run's terminal event is still buffered, and nothing re-enters
   * this method when that event lands.
   */
  #onControlState(state: ControlChannelState): void {
    if (this.#closed) return;
    if (sameControlChannelState(this.#controls, state)) return;
    this.#controls = state;
    this.#publish();
  }

  /**
   * The browser transport, wired to report control-channel connectivity here
   * and to redial on the session's own reconnect cadence, so the command path
   * and the event stream back off on one schedule.
   */
  #browserTransport(
    hooks: WebSessionTransportHooks,
    reconnectDelaysMs: readonly number[] | undefined,
  ): ControlTransport {
    return new WebSocketTransport(webSocketUrlFromLocation(window.location), {
      onConnectionState: hooks.onConnectionState,
      ...(reconnectDelaysMs === undefined ? {} : {reconnectDelaysMs}),
    });
  }

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
    this.#publish();
  }

  /**
   * Republish the whole state as one immutable value, so a
   * `useSyncExternalStore` consumer compares one reference rather than tracking
   * fields.
   */
  #publish(): void {
    this.#state = {status: this.#status, error: this.#error, controls: this.#controls};
    for (const listener of this.#listeners) listener();
  }
}

export function webSocketUrlFromLocation(location: Location): string {
  const page = new URL(location.href);
  const gateway = page.searchParams.get('gateway');
  const url = gateway === null ? page : new URL(gateway, page.origin);
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  url.pathname = '/ws';
  // A capability token is a bearer credential for one authority, so it is read
  // only from the query of the URL that names the socket's own authority: the
  // page when there is no `?gateway=`, and otherwise the `?gateway=` value,
  // which must carry its own token just as the in-app gateway form requires.
  const token = url.searchParams.get('token') ?? '';
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
