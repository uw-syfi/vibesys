/**
 * The run page's one path to the backend. It is the only module of `apps/web`, and of the features
 * it composes, that imports @vibesys/transport's runtime values or calls `fetch`
 * (`clients/.dependency-cruiser.mjs` and `clients/tools/lint-rules/web-fetch-only-in-backend.grit` hold
 * it to that): the control transport (a WebSocket, or the desktop shell's bridge shaped as one)
 * that `@vibesys/run-session`'s `connectRun` runs the run over, the gateway's capability exchange,
 * and the replay fixture. It is also the
 * entry of the browser transport bundle that `check:web-browser-bundle` keeps free of Node builtins.
 */

import {type RunEvent, validateRunEvent} from '@vibesys/contracts';
import type {TransportHooks} from '@vibesys/run-session';
import type {ControlTransport, RunChannelRole, ScheduleTimeout} from '@vibesys/transport';
import {type WebSocketLike, WebSocketTransport} from '@vibesys/transport/websocket';

export type {ControlTransport, WebSocketLike};

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
  webSocket?: (role: RunChannelRole) => WebSocketLike,
): ControlTransport {
  const {scheduleTimeout, reconnectDelaysMs} = timing;
  return new WebSocketTransport(url, {
    onConnectionState: hooks.onConnectionState,
    ...(webSocket === undefined
      ? {}
      : {webSocket: (_url: string, role: RunChannelRole) => webSocket(role)}),
    ...(scheduleTimeout === undefined ? {} : {scheduleTimeout}),
    ...(reconnectDelaysMs === undefined ? {} : {reconnectDelaysMs}),
  });
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
