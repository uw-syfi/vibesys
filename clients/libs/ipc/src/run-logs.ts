/**
 * A page's logs requests: what a view's `logs` sends the main process, and what comes back. A
 * listing names the run's instance, and a read must name the same one. A read carries a token the page chose, unique among its reads in
 * flight, so the page can `withdraw` it when it stops waiting: an IPC invoke cannot be withdrawn
 * itself, and the main process then withdraws the helper's long poll.
 */
import type {LogListOutcome, LogReadInput, LogReadOutcome} from '@vibesys/logs';

/** The longest read token a page may choose. */
const LOG_READ_TOKEN_MAX_LENGTH = 64;
const LOG_READ_TOKEN = new RegExp(`^[A-Za-z0-9_-]{1,${LOG_READ_TOKEN_MAX_LENGTH}}$`);

export type RunLogsRequest =
  | {readonly kind: 'list'}
  | {readonly kind: 'read'; readonly token: string; readonly read: LogReadInput}
  /** Abort the page's read in flight named `token`; a token naming none (it answered) is a no-op. */
  | {readonly kind: 'withdraw'; readonly token: string};

/** The reply to each request kind: a withdrawal answers `null` once the main process acted on it. */
export type RunLogsReply<R extends RunLogsRequest> = R extends {readonly kind: 'list'}
  ? LogListOutcome
  : R extends {readonly kind: 'read'}
    ? LogReadOutcome
    : null;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function count(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0;
}

function onlyKeys(value: Record<string, unknown>, keys: readonly string[]): boolean {
  return Object.keys(value).every(key => keys.includes(key));
}

function token(value: unknown): value is string {
  return typeof value === 'string' && LOG_READ_TOKEN.test(value);
}

function readStart(value: unknown): LogReadInput['start'] | null {
  if (!isRecord(value)) return null;
  if (onlyKeys(value, ['offset']) && count(value['offset'])) return {offset: value['offset']};
  if (onlyKeys(value, ['tail_bytes']) && count(value['tail_bytes']))
    return {tail_bytes: value['tail_bytes']};
  return null;
}

function readInput(value: unknown): LogReadInput | null {
  if (
    !isRecord(value) ||
    !onlyKeys(value, ['instance_id', 'stream_id', 'start', 'max_bytes', 'wait_ms'])
  )
    return null;
  const {instance_id, stream_id, max_bytes, wait_ms} = value;
  const start = readStart(value['start']);
  if (
    typeof instance_id !== 'string' ||
    typeof stream_id !== 'string' ||
    start === null ||
    !count(max_bytes) ||
    !count(wait_ms)
  )
    return null;
  return {instance_id, stream_id, start, max_bytes, wait_ms};
}

/**
 * The page's request, or null when it is not one the page may send. The helper validates the
 * bounds again; this keeps an unknown key or a wrong type from crossing into the main process.
 */
export function parseRunLogsRequest(value: unknown): RunLogsRequest | null {
  if (!isRecord(value)) return null;
  if (value['kind'] === 'list' && onlyKeys(value, ['kind'])) return {kind: 'list'};
  if (value['kind'] === 'withdraw' && onlyKeys(value, ['kind', 'token'])) {
    return token(value['token']) ? {kind: 'withdraw', token: value['token']} : null;
  }
  if (value['kind'] !== 'read' || !onlyKeys(value, ['kind', 'token', 'read'])) return null;
  const read = readInput(value['read']);
  if (read === null || !token(value['token'])) return null;
  return {kind: 'read', token: value['token'], read};
}
