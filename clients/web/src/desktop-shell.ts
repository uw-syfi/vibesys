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
import type {ScheduleTimeout} from '@vibesys/backend-client';
import type {WebSocketLike} from '@vibesys/backend-client/websocket';
import {WebSocketTransport} from './browser-entry.js';
import type {BrowserLifecycle, WebSessionOptions} from './session.js';

/** The platforms the desktop shell reports (`process.platform` values it supports). */
const PLATFORMS = ['darwin', 'win32', 'linux'] as const;

type DesktopPlatform = (typeof PLATFORMS)[number];

/** What the page hears about one connection. Each callback runs on the page's event loop. */
export interface DesktopSocketHandlers {
  /** The connection to the server is up; at most once, before any message. */
  onOpen(): void;
  /** One protocol message from the server. */
  onMessage(data: string): void;
  /** The connection ended, or could not be opened; exactly once unless the page closed it. */
  onClose(): void;
}

/** The page's end of one connection. */
export interface DesktopSocket {
  /** Send one protocol message; only after `onOpen`. */
  send(data: string): void;
  close(): void;
}

/** The connection half of the bridge. */
export interface DesktopBridge {
  connect(handlers: DesktopSocketHandlers): DesktopSocket;
  onWake(listener: () => void): void;
}

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
  const known = PLATFORMS.find(candidate => candidate === platform);
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
    connect: handlers => {
      const socket: unknown = connect(handlers);
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
function desktopWebSocket(bridge: DesktopBridge): WebSocketLike {
  return new BridgeSocket(bridge);
}

/**
 * `base`, plus the shell's wake reports delivered as `online`: both mean "the network may be back,
 * retry now", which is the one thing the session does with either.
 */
function desktopLifecycle(base: BrowserLifecycle, bridge: DesktopBridge): BrowserLifecycle {
  const online = new Set<() => void>();
  bridge.onWake(() => {
    for (const listener of [...online]) listener();
  });
  return {
    get visibilityState() {
      return base.visibilityState;
    },
    get online() {
      return base.online;
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

/**
 * The `WebSession` wiring for a page inside the desktop shell: the browser's `WebSocketTransport`
 * over bridge connections, and `lifecycle` plus the shell's wake reports. Everything above the
 * socket (redial, resume, reconciliation) is the browser's own code.
 */
export function desktopSessionOptions(
  bridge: DesktopBridge,
  lifecycle: BrowserLifecycle,
  timing: DesktopSessionTiming = {},
): WebSessionOptions {
  const {scheduleTimeout, reconnectDelaysMs} = timing;
  return {
    lifecycle: desktopLifecycle(lifecycle, bridge),
    ...(scheduleTimeout === undefined ? {} : {scheduleTimeout}),
    ...(reconnectDelaysMs === undefined ? {} : {reconnectDelaysMs}),
    // The shell binds the window to its server, so the transport's URL names nothing; it is kept
    // only because the transport's constructor takes one.
    transport: hooks =>
      new WebSocketTransport('vibesys-desktop:', {
        onConnectionState: hooks.onConnectionState,
        webSocket: () => desktopWebSocket(bridge),
        ...(scheduleTimeout === undefined ? {} : {scheduleTimeout}),
        ...(reconnectDelaysMs === undefined ? {} : {reconnectDelaysMs}),
      }),
  };
}

/** Timing seams; the defaults are the browser's. Tests supply a deterministic scheduler. */
export interface DesktopSessionTiming {
  readonly scheduleTimeout?: ScheduleTimeout;
  readonly reconnectDelaysMs?: readonly number[];
}
