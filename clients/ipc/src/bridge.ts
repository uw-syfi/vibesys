/**
 * The run page's bridge: what the run preload exposes to the page (`window.vibesysDesktop`), and
 * the messages the preload and the main process exchange over one connection's MessagePort.
 *
 * One port is one protocol connection, with WebSocket's shape: the main process reports `open`
 * once the host stream is up, then one `message` per protocol frame (the server's line without its
 * newline, per WP-NEWLINE in docs/contributing/wire-protocol.md), and `close` exactly once. The page
 * sends one `send` per protocol message and `close` when it is done. The preload cannot import
 * values (it is sandboxed), so the channel names are repeated there; these types keep both sides
 * honest.
 */

/** The platforms the desktop shell reports (`process.platform` values it supports). */
export const DESKTOP_PLATFORMS = ['darwin', 'win32', 'linux'] as const;

export type DesktopPlatform = (typeof DESKTOP_PLATFORMS)[number];

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

/** What the run preload exposes on `window.vibesysDesktop`. */
export interface DesktopPreload extends DesktopBridge {
  readonly platform: string;
}

/** IPC channel on which the preload hands the main process a new connection's port. */
export const CONNECT_CHANNEL = 'vibesys:connect';
/** IPC channel on which the main process tells the page that the machine woke from sleep. */
export const WAKE_CHANNEL = 'vibesys:wake';

export type MainToPage =
  | {readonly type: 'open'}
  | {readonly type: 'message'; readonly data: string}
  | {readonly type: 'close'};

export type PageToMain = {readonly type: 'send'; readonly data: string} | {readonly type: 'close'};

/** The page's message, or null when it is not one the page may send. */
export function parsePageMessage(value: unknown): PageToMain | null {
  if (typeof value !== 'object' || value === null) return null;
  const {type, data} = value as {readonly type?: unknown; readonly data?: unknown};
  if (type === 'close') return {type};
  if (type === 'send' && typeof data === 'string') return {type, data};
  return null;
}
