/**
 * The run page's one path to the backend. It is the only module of `clients/web` that imports
 * backend-client's runtime values or calls `fetch` (`clients/.dependency-cruiser.mjs` and
 * `clients/lint-rules/web-fetch-only-in-backend.grit` hold it to that): the control transport
 * (a WebSocket, or the desktop shell's bridge shaped as one), the run's event stream and snapshot
 * over it, the gateway's capability exchange, and the replay fixture. It is also the entry of the
 * browser transport bundle that `check:web-browser-bundle` keeps free of Node builtins.
 */
import {
  type BatchReconciliation,
  type ControlChannelState,
  type ControlTransport,
  type EventBatchMessage,
  PersistentEventStream,
  type PersistentEventStreamOptions,
  type RunEvent,
  type RunSnapshot,
  type ScheduleTimeout,
  ServerError,
  type StreamConnectionState,
  type StreamMessageContext,
  type StreamRebootstrapDisposition,
  StreamReconciler,
  sameControlChannelState,
  validateRunEvent,
} from '@vibesys/backend-client';
import {type WebSocketLike, WebSocketTransport} from '@vibesys/backend-client/websocket';

export type {ControlTransport, WebSocketLike};

/** Whether a fenced snapshot failed because its stream store was replaced. */
export function isSnapshotStoreReplaced(error: unknown): error is Error {
  return error instanceof ServerError && error.diagnostic?.code === 'snapshot_store_replaced';
}

/** Where a transport reports control-channel connectivity. */
export interface TransportHooks {
  readonly onConnectionState: (state: ControlChannelState) => void;
}

/** Timing seams; the defaults are the browser's. Tests supply a deterministic scheduler. */
export interface TransportTiming {
  readonly scheduleTimeout?: ScheduleTimeout;
  readonly reconnectDelaysMs?: readonly number[];
}

/**
 * The control transport over `url`, or over the sockets `webSocket` opens (the desktop bridge),
 * redialing on `timing`'s cadence so the command path and the event stream back off on one
 * schedule.
 */
export function openTransport(
  url: string,
  hooks: TransportHooks,
  timing: TransportTiming = {},
  webSocket?: () => WebSocketLike,
): ControlTransport {
  const {scheduleTimeout, reconnectDelaysMs} = timing;
  return new WebSocketTransport(url, {
    onConnectionState: hooks.onConnectionState,
    ...(webSocket === undefined ? {} : {webSocket}),
    ...(scheduleTimeout === undefined ? {} : {scheduleTimeout}),
    ...(reconnectDelaysMs === undefined ? {} : {reconnectDelaysMs}),
  });
}

/** What the run's event stream reports to the session. */
export interface RunStreamHandlers {
  /** The last sequence folded, where a resumed subscription continues. */
  cursor(): number;
  /** Whether the caller's fold can still be incomplete, so a dropped stream is worth redialing. */
  shouldReconnect(): boolean;
  onBatch(
    message: EventBatchMessage,
    reconciliation: BatchReconciliation,
    context: StreamMessageContext,
  ): void;
  /** An in-band protocol error; the transport suppresses the close that follows it. */
  onFault(error: Error): void;
  onConnectionState(state: StreamConnectionState): void;
}

/** The run behind one control transport: its snapshot and its persistent event stream. */
export interface RunConnection {
  /** Load an unfenced snapshot, or require the event store established by a stream batch. */
  loadSnapshot(storeId?: string): Promise<RunSnapshot>;
  /** Subscribe once; settles when the first subscription does. */
  subscribe(handlers: RunStreamHandlers): Promise<void>;
  /** Resume the stream where it was. */
  retry(): void;
  /** Retry only when the currently owned stream has already disconnected. */
  retryIfDisconnected(): void;
  /** Restart the stream from a fresh bootstrap. */
  rebootstrap(): StreamRebootstrapDisposition;
  /** Cancel an armed control-channel redial and dial now (inert when healthy or dialing). */
  redialControls(): void;
  close(): Promise<void>;
}

export interface RunConnectionOptions extends TransportTiming {
  readonly tail?: number;
}

/**
 * How a session opens its run: the transport `open` builds (reporting to the session's hooks, with
 * repeats of one control-channel state dropped), and the run connection over it.
 */
export function connectRun(
  open: (hooks: TransportHooks) => ControlTransport,
  options: RunConnectionOptions = {},
): (hooks: TransportHooks) => RunConnection {
  return hooks => {
    let last: ControlChannelState = {status: 'connected'};
    const transport = open({
      onConnectionState: state => {
        if (sameControlChannelState(last, state)) return;
        last = state;
        hooks.onConnectionState(state);
      },
    });
    return openRunConnection(transport, options);
  };
}

function openRunConnection(
  transport: ControlTransport,
  options: RunConnectionOptions = {},
): RunConnection {
  const {tail, reconnectDelaysMs, scheduleTimeout} = options;
  const streamOptions: PersistentEventStreamOptions = {
    ...(tail === undefined ? {} : {tail}),
    ...(reconnectDelaysMs === undefined ? {} : {reconnectDelaysMs}),
    ...(scheduleTimeout === undefined ? {} : {scheduleTimeout}),
  };
  const stream = new PersistentEventStream(transport, streamOptions);
  // The browser replays the whole log and exposes no backfill action, so the positive chunk is
  // dormant; it follows the configured tail so a future history action cannot drift from it.
  const reconciler = new StreamReconciler({backfillChunk: tail ?? 1});
  return {
    async loadSnapshot(storeId) {
      const response = await transport.request({
        type: 'query.snapshot',
        ...(storeId === undefined ? {} : {expected_store_id: storeId}),
      });
      if (response.ok !== true || response.snapshot === null || response.snapshot === undefined) {
        throw new ServerError(
          response.error ?? 'Server did not return a run snapshot',
          response.diagnostic ?? null,
        );
      }
      return response.snapshot;
    },
    subscribe(handlers) {
      return stream.subscribe({
        cursor: handlers.cursor,
        storeId: () => reconciler.storeId(),
        shouldReconnect: handlers.shouldReconnect,
        onMessage: (message, context) => {
          if (message.type === 'event_batch') {
            handlers.onBatch(message, reconciler.reconcileBatch(message, context), context);
          } else if (message.type === 'protocol_error') {
            handlers.onFault(new ServerError(message.message, message.diagnostic ?? null));
          }
        },
        onConnectionState: handlers.onConnectionState,
      });
    },
    retry: () => stream.retry(),
    retryIfDisconnected: () => stream.retryIfDisconnected(),
    rebootstrap: () => stream.rebootstrap(),
    redialControls: () => transport.reconnect(),
    async close() {
      await stream.close();
      await transport.close();
    },
  };
}

const BROWSER_SESSION_HEADER = 'X-VibeSys-Browser-Session';

export type Fetch = (input: string, init?: RequestInit) => Promise<Response>;

export const DEFAULT_GATEWAY_BOOTSTRAP_TIMEOUT_MS = 10_000;

export interface GatewayBootstrapOptions {
  /** Stop when the feature no longer owns this exchange. */
  readonly signal?: AbortSignal;
  /** Test seam for the HTTP boundary. */
  readonly fetcher?: Fetch;
  /** Test seam for the response deadline. */
  readonly scheduleTimeout?: ScheduleTimeout;
  readonly timeoutMs?: number;
}

/** Exchange a gateway launch capability for this tab's browser-session credential. */
export async function bootstrapGateway(
  capabilityUrl: string,
  options: GatewayBootstrapOptions = {},
): Promise<string> {
  const timeoutMs = options.timeoutMs ?? DEFAULT_GATEWAY_BOOTSTRAP_TIMEOUT_MS;
  if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) {
    throw new RangeError('Gateway bootstrap timeout must be a positive finite number');
  }
  const controller = new AbortController();
  const stopForwardingAbort = forwardAbort(options.signal, controller);
  let cancelDeadline = (): void => undefined;
  try {
    cancelDeadline = (options.scheduleTimeout ?? scheduleBrowserTimeout)(() => {
      controller.abort(new Error(`Gateway connection timed out after ${timeoutMs} ms`));
    }, timeoutMs);
    const response = await fetchUntilAbort(
      options.fetcher ?? fetch,
      capabilityUrl,
      {
        cache: 'no-store',
        credentials: 'include',
        mode: 'cors',
        signal: controller.signal,
      },
      controller.signal,
    );
    if (!response.ok) throw new Error(`Gateway rejected the capability (HTTP ${response.status})`);
    const browserSession = response.headers.get(BROWSER_SESSION_HEADER);
    if (browserSession === null || browserSession === '') {
      throw new Error('Gateway did not establish a browser session');
    }
    return browserSession;
  } finally {
    cancelDeadline();
    stopForwardingAbort();
  }
}

const scheduleBrowserTimeout: ScheduleTimeout = (callback, delayMs) => {
  const timer = setTimeout(callback, delayMs);
  return () => clearTimeout(timer);
};

function forwardAbort(source: AbortSignal | undefined, target: AbortController): () => void {
  if (source === undefined) return () => undefined;
  const abort = (): void => target.abort(abortReason(source));
  if (source.aborted) {
    abort();
    return () => undefined;
  }
  source.addEventListener('abort', abort, {once: true});
  return () => source.removeEventListener('abort', abort);
}

/** Settle on ownership loss even when a structural fetch Fake ignores its signal. */
function fetchUntilAbort(
  fetcher: Fetch,
  capabilityUrl: string,
  init: RequestInit,
  signal: AbortSignal,
): Promise<Response> {
  if (signal.aborted) return Promise.reject(abortReason(signal));
  return new Promise<Response>((resolve, reject) => {
    let settled = false;
    const finish = (complete: () => void): void => {
      if (settled) return;
      settled = true;
      signal.removeEventListener('abort', aborted);
      complete();
    };
    const aborted = (): void => finish(() => reject(abortReason(signal)));
    signal.addEventListener('abort', aborted, {once: true});
    let request: Promise<Response>;
    try {
      request = fetcher(capabilityUrl, init);
    } catch (reason) {
      finish(() => reject(reason));
      return;
    }
    void request.then(
      response => finish(() => resolve(response)),
      reason => finish(() => reject(reason)),
    );
  });
}

function abortReason(signal: AbortSignal): Error {
  if (signal.reason instanceof Error) return signal.reason;
  return new DOMException('Gateway connection was aborted', 'AbortError');
}

/** Fetch a JSONL replay fixture and validate every event; any bad line rejects the whole file. */
export async function fetchReplayFixture(url: string, fetcher: Fetch = fetch): Promise<RunEvent[]> {
  const response = await fetcher(url);
  if (!response.ok) throw new Error(`Replay fixture request failed with ${response.status}`);
  const events: RunEvent[] = [];
  for (const [index, line] of (await response.text()).split('\n').entries()) {
    if (line.trim().length === 0) continue;
    const lineNumber = index + 1;
    let value: unknown;
    try {
      value = JSON.parse(line);
    } catch (reason) {
      throw new Error(`Replay fixture contains invalid JSON on line ${lineNumber}`, {
        cause: reason,
      });
    }
    try {
      events.push(validateRunEvent(value));
    } catch (reason) {
      const detail = reason instanceof Error ? `: ${reason.message}` : '';
      throw new Error(`Replay fixture contains invalid event on line ${lineNumber}${detail}`, {
        cause: reason,
      });
    }
  }
  return events;
}
