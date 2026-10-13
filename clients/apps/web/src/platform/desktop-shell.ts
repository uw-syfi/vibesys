/**
 * Detects the Electron desktop shell, describes it to the stylesheet, and adapts its bridge.
 *
 * The shell's sandboxed preload exposes `window.vibesysDesktop`; this module is the only place that
 * reads it. In a plain browser the global is absent and the page is left exactly as it was.
 *
 * The bridge carries protocol connections: `connect()` opens one connection to the server the
 * shell bound this window to (the page cannot name a host or a path), and `onWake()` reports the
 * machine waking from sleep. `desktopWebSocket` turns a connection into the `WebSocketLike` the
 * browser transport already drives, so redial, resume, and reconciliation run unchanged.
 */

import {
  DESKTOP_PLATFORMS,
  type DesktopBridge,
  type DesktopPlatform,
  type DesktopSocket,
  type RunChannelRole,
} from '@vibesys/ipc';
import {
  type BrowserLifecycle,
  connectRun,
  type RunConnection,
  type TransportHooks,
} from '@vibesys/run-session';
import {openTransport, type TransportTiming, type WebSocketLike} from './backend.js';

/** What the desktop shell's preload exposes on `window`. */
interface VibesysDesktop extends Partial<DesktopBridge> {
  readonly platform: DesktopPlatform;
}

declare global {
  interface Window {
    vibesysDesktop?: VibesysDesktop;
  }
}

/** The `<html>` attributes that switch on desktop-only styling. */
export interface DesktopShellAttributes {
  readonly 'data-shell': 'desktop';
  readonly 'data-platform': DesktopPlatform;
}

/** The attributes for a bridge value, or `null` when it is not a recognized desktop shell. */
export function desktopShellAttributes(bridge: unknown): DesktopShellAttributes | null {
  if (typeof bridge !== 'object' || bridge === null) return null;
  const platform: unknown = (bridge as {readonly platform?: unknown}).platform;
  const known = DESKTOP_PLATFORMS.find(candidate => candidate === platform);
  return known === undefined ? null : {'data-shell': 'desktop', 'data-platform': known};
}

/** Mark `root` for desktop-only styling when the page runs inside the desktop shell. */
export function markDesktopShell(root: HTMLElement, bridge: unknown): void {
  const attributes = desktopShellAttributes(bridge);
  if (attributes === null) return;
  for (const [name, value] of Object.entries(attributes)) root.setAttribute(name, value);
}

/** The bridge's connection half, or `null` when `bridge` does not carry one. */
export function desktopBridge(bridge: unknown): DesktopBridge | null {
  if (desktopShellAttributes(bridge) === null) return null;
  const {connect, onWake} = bridge as {readonly connect?: unknown; readonly onWake?: unknown};
  if (typeof connect !== 'function' || typeof onWake !== 'function') return null;
  return {
    connect: (role, handlers) => {
      const socket: unknown = connect(role, handlers);
      const {send, close} = (socket ?? {}) as {readonly send?: unknown; readonly close?: unknown};
      if (typeof send !== 'function' || typeof close !== 'function') {
        throw new Error('The desktop bridge returned an unusable connection');
      }
      return {send: data => send(data), close: () => close()};
    },
    onWake: listener => onWake(listener),
  };
}

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

  constructor(bridge: DesktopBridge, role: RunChannelRole) {
    this.#socket = bridge.connect(role, {
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
    if (this.#readyState !== OPEN) throw new Error('The desktop connection is not open');
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

/** Open a bridge connection shaped as the socket `WebSocketTransport` drives. */
function desktopWebSocket(bridge: DesktopBridge, role: RunChannelRole): WebSocketLike {
  return new BridgeSocket(bridge, role);
}

/**
 * `base`, plus the shell's wake reports delivered as `online`: both mean "the network may be back,
 * retry now", which is the one thing the session does with either.
 */
function desktopLifecycle(base: BrowserLifecycle, bridge: DesktopBridge): BrowserLifecycle {
  const online = new Set<() => void>();
  let shellReachable = false;
  bridge.onWake(() => {
    // Main emits this only after its host connection is ready. Expose that stronger evidence while
    // dispatching the event, then return to the browser's generic reachability for later events.
    const previous = shellReachable;
    shellReachable = true;
    try {
      for (const listener of [...online]) listener();
    } finally {
      shellReachable = previous;
    }
  });
  return {
    get visibilityState() {
      return base.visibilityState;
    },
    get online() {
      return shellReachable || base.online;
    },
    addEventListener(type, listener) {
      if (type === 'online') online.add(listener);
      base.addEventListener(type, listener);
    },
    removeEventListener(type, listener) {
      if (type === 'online') online.delete(listener);
      base.removeEventListener(type, listener);
    },
  };
}

/** What a live session needs from the desktop shell: its lifecycle, transport, and timing. */
export interface DesktopSessionWiring {
  readonly lifecycle: BrowserLifecycle;
  readonly connect: (hooks: TransportHooks) => RunConnection;
}

/**
 * The page keeps no retry timer of its own inside the desktop shell: the main process's connection
 * manager is the one retry loop per host (`docs/contributing/desktop-connection.md`). A dial waits
 * there until the host is ready or the manager gives up, and the shell's wake (`onWake`) redials
 * once the host recovers.
 */
const DESKTOP_TIMING: TransportTiming = {reconnectDelaysMs: []};

/**
 * The `WebSession` wiring for a page inside the desktop shell: the browser's `WebSocketTransport`
 * over bridge connections, and `lifecycle` plus the shell's wake reports. Everything above the
 * socket (resume, reconciliation) is the browser's own code; redial happens on a wake.
 */
export function desktopSessionOptions(
  bridge: DesktopBridge,
  lifecycle: BrowserLifecycle,
  timing: TransportTiming = DESKTOP_TIMING,
): DesktopSessionWiring {
  return {
    lifecycle: desktopLifecycle(lifecycle, bridge),
    // The shell binds the window to its server, so the transport's URL names nothing; it is kept
    // only because the transport's constructor takes one.
    connect: connectRun(
      hooks =>
        openTransport('vibesys-desktop:', hooks, timing, role => desktopWebSocket(bridge, role)),
      timing,
    ),
  };
}
