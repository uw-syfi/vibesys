/**
 * Decoding a `run_narration` stream: each line is one `NarrationLine` as JSON
 * (`docs/contributing/logs.md`). A line that is not one (a write cut short, a line from a newer
 * writer, plain text) decodes as `raw`, so a view shows it as it is instead of dropping it.
 */
import type {NarrationLevel, NarrationLine} from '@vibesys/contracts';

export type NarrationRecord =
  | {readonly kind: 'line'; readonly line: NarrationLine}
  | {readonly kind: 'raw'; readonly text: string};

const LEVELS: ReadonlySet<string> = new Set<NarrationLevel>([
  'DEBUG',
  'INFO',
  'WARNING',
  'ERROR',
  'CRITICAL',
]);

const KEYS: ReadonlySet<string> = new Set([
  'at',
  'level',
  'logger',
  'thread',
  'message',
  'traceback',
]);

function isLevel(value: unknown): value is NarrationLevel {
  return typeof value === 'string' && LEVELS.has(value);
}

function parsed(text: string): unknown {
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return undefined;
  }
}

/**
 * `text` as a narration record, or `raw` when it is not exactly one. The writer forbids unknown
 * keys, so a line with one is not a record this view knows how to read, and shows raw.
 */
export function decodeNarrationLine(text: string): NarrationRecord {
  const raw: NarrationRecord = {kind: 'raw', text};
  const value = parsed(text);
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return raw;
  const record = value as Record<string, unknown>;
  if (!Object.keys(record).every(key => KEYS.has(key))) return raw;
  const {at, level, logger, thread, message, traceback} = record;
  if (
    typeof at !== 'string' ||
    !isLevel(level) ||
    typeof logger !== 'string' ||
    typeof thread !== 'string' ||
    typeof message !== 'string' ||
    (traceback !== undefined && traceback !== null && typeof traceback !== 'string')
  )
    return raw;
  return {
    kind: 'line',
    line: {at, level, logger, thread, message, traceback: traceback ?? null},
  };
}
