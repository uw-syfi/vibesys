/**
 * The run page's logs requests: what the run preload's `logs` sends the main process on
 * `LOGS_CHANNEL`, and what comes back. The page names no host and no channel: the main process
 * serves each request from the run the page is bound to, over that host's logs channel
 * (`docs/contributing/desktop-connection.md`, "Logs"). A listing names that run's instance, and a
 * read must name the same one.
 */
import type {LogListOutcome, LogReadInput, LogReadOutcome} from '@vibesys/logs';

/** IPC channel on which the run page asks the main process for its run's logs (`invoke`). */
export const LOGS_CHANNEL = 'vibesys:logs';

export type RunLogsRequest =
  | {readonly kind: 'list'}
  | {readonly kind: 'read'; readonly read: LogReadInput};

/** The reply to each request kind. */
export type RunLogsReply<R extends RunLogsRequest> = R extends {readonly kind: 'list'}
  ? LogListOutcome
  : LogReadOutcome;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function count(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0;
}

function onlyKeys(value: Record<string, unknown>, keys: readonly string[]): boolean {
  return Object.keys(value).every(key => keys.includes(key));
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
  if (value['kind'] !== 'read' || !onlyKeys(value, ['kind', 'read'])) return null;
  const read = readInput(value['read']);
  return read === null ? null : {kind: 'read', read};
}
