/**
 * The messages the preload and the main process exchange over one connection's MessagePort.
 *
 * One port is one protocol connection, with WebSocket's shape: the main process reports `open`
 * once the host stream is up, then one `message` per protocol frame (the server's line without its
 * newline, per WP-NEWLINE in docs/contributing/wire-protocol.md), and `close` exactly once. Each
 * side spends byte credit before sending a frame and returns it only after its consumer accepts
 * the bytes. The page sends one `send` per protocol message and `close` when it is done. The preload
 * cannot import values (it is sandboxed), so the channel names are repeated there; these types keep
 * both sides honest.
 */

/** IPC channel on which the preload hands the main process a new connection's port. */
export const CONNECT_CHANNEL = 'vibesys:connect';
/** IPC channel on which the main process tells the page that the machine woke from sleep. */
export const WAKE_CHANNEL = 'vibesys:wake';

/**
 * Bytes either side may have in flight across one bridge connection.
 *
 * This also admits the newline framer's largest valid remainder after worst-case UTF-8 expansion.
 * The relay still rejects an individual or queued burst above the window instead of buffering it.
 */
export const BRIDGE_CREDIT_WINDOW_BYTES = 16 * 1024 * 1024;

export type MainToPage =
  | {readonly type: 'open'; readonly creditBytes: number}
  | {readonly type: 'message'; readonly data: string; readonly bytes: number}
  | {readonly type: 'credit'; readonly bytes: number}
  | {readonly type: 'close'};

export type PageToMain =
  | {readonly type: 'send'; readonly data: string}
  | {readonly type: 'credit'; readonly bytes: number}
  | {readonly type: 'close'};

/** The page's message, or null when it is not one the page may send. */
export function parsePageMessage(value: unknown): PageToMain | null {
  if (typeof value !== 'object' || value === null) return null;
  const {type, data, bytes} = value as {
    readonly type?: unknown;
    readonly data?: unknown;
    readonly bytes?: unknown;
  };
  if (type === 'close') return {type};
  if (type === 'send' && typeof data === 'string') return {type, data};
  if (type === 'credit' && Number.isSafeInteger(bytes) && Number(bytes) > 0) {
    return {type, bytes: Number(bytes)};
  }
  return null;
}
