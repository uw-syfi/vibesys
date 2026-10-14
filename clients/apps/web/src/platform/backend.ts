/**
 * The run page's one path to the backend. It is the only module of `apps/web`, and of the features
 * it composes, that imports @vibesys/transport's runtime values or calls `fetch`
 * (`clients/.dependency-cruiser.mjs` and `clients/tools/lint-rules/web-fetch-only-in-backend.grit` hold
 * it to that): the control transport (a WebSocket, or the desktop shell's bridge shaped as one)
 * that `@vibesys/run-session`'s `connectRun` runs the run over, the gateway's capability exchange,
 * and the campaign fixture. It is also the
 * entry of the browser transport bundle that `check:web-browser-bundle` keeps free of Node builtins.
 */

import {
  type CampaignFrame,
  type CampaignRecord,
  type CampaignStream,
  parseCampaignFrame,
  parseReplayScenario,
} from '@vibesys/feature-campaign/state';
import type {TransportHooks} from '@vibesys/run-session';
import type {ControlTransport, ScheduleTimeout} from '@vibesys/transport';
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

/** Load and validate the source-backed campaign record at the browser's I/O boundary. */
export async function fetchCampaignFixture(
  signal: AbortSignal,
  fetcher: Fetch = fetch,
): Promise<CampaignRecord> {
  const url = new URL('../../dev/fixtures/trajectory-replay.json', import.meta.url).href;
  const response = await fetcher(url, {signal, cache: 'no-store'});
  if (!response.ok) throw new Error(`Campaign fixture request failed with ${response.status}`);
  return parseReplayScenario(await response.json());
}

interface CampaignEventSource {
  onmessage: ((event: {readonly data: string}) => void) | null;
  onerror: ((event: unknown) => void) | null;
  readonly readyState: number;
  close(): void;
}

/** Parsed SSE frames; unreadable input ends the subscription, and owner close releases it once. */
export class EventSourceCampaignStream implements CampaignStream {
  constructor(
    private readonly url: string,
    private readonly open: (url: string) => CampaignEventSource = url =>
      new EventSource(url) as unknown as CampaignEventSource,
  ) {}

  subscribe(onFrame: (frame: CampaignFrame) => void, onError: (error: Error) => void): () => void {
    const source = this.open(this.url);
    let released = false;
    const release = (): boolean => {
      if (released) return false;
      released = true;
      source.onmessage = null;
      source.onerror = null;
      source.close();
      return true;
    };
    const fail = (error: Error): void => {
      if (release()) onError(error);
    };
    source.onmessage = event => {
      if (released) return;
      let frame: CampaignFrame;
      try {
        frame = parseCampaignFrame(JSON.parse(event.data));
      } catch (error) {
        fail(error instanceof Error ? error : new Error(String(error)));
        return;
      }
      onFrame(frame);
    };
    source.onerror = () => {
      // CONNECTING is recoverable: EventSource retries and the fold ignores replayed sequences.
      if (source.readyState === 2) fail(new Error('Campaign stream connection error'));
    };
    return () => void release();
  }
}
