/**
 * The Run screen's session transport in the app shell: the shell's run connection (a
 * `DesktopBridge` over the window's relay, which the Run screen builds from the welcome bridge)
 * shaped as the `WebSocketLike` the browser transport drives, and the `WebSession` wiring over it.
 * The app owns this wiring because it is the place that may build a transport (features use the
 * transport's types only). The page keeps no redial timer: the main process's connection manager is
 * the one retry loop per host, and its wake redials.
 *
 * The bridge-to-socket adapter connects the desktop shell to a run through the main process.
 */
import type {DesktopBridge, DesktopSocket} from '@vibesys/ipc';
import {
  type BrowserLifecycle,
  connectRun,
  type PageVisibility,
  type WebSessionOptions,
} from '@vibesys/run-session';
import {type WebSocketLike, WebSocketTransport} from '@vibesys/transport/websocket';

const CONNECTING = 0;
const OPEN = 1;
const CLOSING = 2;
const CLOSED = 3;

/** One bridge connection with a WebSocket's lifecycle: `onclose` runs exactly once. */
class BridgeSocket implements WebSocketLike {
  onopen: (() => void) | null = null;
  onmessage: ((event: {readonly data: unknown}) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: (() => void) | null = null;
  #readyState = CONNECTING;
  readonly #socket: DesktopSocket;

  constructor(bridge: DesktopBridge) {
    this.#socket = bridge.connect({
      onOpen: () => {
        if (this.#readyState !== CONNECTING) return;
        this.#readyState = OPEN;
        this.onopen?.();
      },
      onMessage: data => {
        if (this.#readyState === OPEN) this.onmessage?.({data});
      },
      onClose: () => this.#ended(),
    });
  }

  get readyState(): number {
    return this.#readyState;
  }

  send(data: string): void {
    // A WebSocket refuses a send before it opens rather than queueing it; so does this.
    if (this.#readyState !== OPEN) throw new Error('The run connection is not open');
    this.#socket.send(data);
  }

  close(): void {
    if (this.#readyState === CLOSING || this.#readyState === CLOSED) return;
    this.#readyState = CLOSING;
    this.#socket.close();
    // A WebSocket reports its own close asynchronously, after `close()` returns.
    queueMicrotask(() => this.#ended());
  }

  #ended(): void {
    if (this.#readyState === CLOSED) return;
    this.#readyState = CLOSED;
    this.onclose?.();
  }
}

/** The page's visibility and network, from the document, window and navigator. */
export interface PageEnvironment {
  readonly document: {
    readonly visibilityState: PageVisibility;
    addEventListener(type: 'visibilitychange', listener: () => void): void;
    removeEventListener(type: 'visibilitychange', listener: () => void): void;
  };
  readonly window: {
    addEventListener(type: 'online' | 'offline', listener: () => void): void;
    removeEventListener(type: 'online' | 'offline', listener: () => void): void;
  };
  readonly navigator: {readonly onLine: boolean};
}

/** The page's lifecycle, plus the shell's wake delivered as `online`: both mean "retry now". */
function shellLifecycle(page: PageEnvironment, bridge: DesktopBridge): BrowserLifecycle {
  const online = new Set<() => void>();
  let woke = false;
  bridge.onWake(() => {
    // The main process wakes the page only once the host is ready again: report it reachable
    // while the wake is delivered, then return to the browser's own reachability.
    woke = true;
    try {
      for (const listener of [...online]) listener();
    } finally {
      woke = false;
    }
  });
  return {
    get visibilityState() {
      return page.document.visibilityState;
    },
    get online() {
      return woke || page.navigator.onLine;
    },
    addEventListener(type, listener) {
      if (type === 'visibilitychange') {
        page.document.addEventListener(type, listener);
        return;
      }
      if (type === 'online') online.add(listener);
      page.window.addEventListener(type, listener);
    },
    removeEventListener(type, listener) {
      if (type === 'visibilitychange') {
        page.document.removeEventListener(type, listener);
        return;
      }
      if (type === 'online') online.delete(listener);
      page.window.removeEventListener(type, listener);
    },
  };
}

/** No redial timer in the page: a dial waits on the main process, and the wake redials. */
const NO_REDIAL: readonly number[] = [];

/** The `WebSession` wiring for the shell's run connection over `bridge`. */
export function shellSessionOptions(
  bridge: DesktopBridge,
  page: PageEnvironment,
): WebSessionOptions {
  return {
    lifecycle: shellLifecycle(page, bridge),
    // The window binds the run, so the transport's URL names nothing; its constructor takes one.
    connect: connectRun(
      hooks =>
        new WebSocketTransport('vibesys-desktop:', {
          onConnectionState: hooks.onConnectionState,
          webSocket: () => new BridgeSocket(bridge),
          reconnectDelaysMs: NO_REDIAL,
        }),
      {reconnectDelaysMs: NO_REDIAL},
    ),
  };
}
