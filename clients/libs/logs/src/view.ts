/**
 * The data fold of one log stream on screen: `LogChunk`s from `logs.read`, in order, folded into a
 * bounded view of lines (`docs/contributing/desktop-connection.md`, "Logs").
 *
 * The view keeps at most `maxLines` complete lines and evicts the oldest past that, counting them,
 * plus the unfinished last line (`partial`), which the next chunk continues. A line longer than
 * `maxLineChars` is wrapped into pieces of that length, wherever its chunks were cut, so memory stays
 * bounded and the lines depend only on the text, not on how it was chunked. A chunk that does not
 * start where the view ended is either stale (it starts earlier, and is dropped) or follows bytes
 * the store evicted (it starts later): then the unfinished line ends, and the skipped bytes are
 * counted, so the view never joins text across a gap.
 */
import type {LogChunk} from '@vibesys/contracts/host-helper';

export interface LogViewBounds {
  /** The most complete lines the view keeps. */
  readonly maxLines: number;
  /** The longest line; a longer one is wrapped into pieces of this many UTF-16 units. */
  readonly maxLineChars: number;
}

export const DEFAULT_LOG_VIEW_BOUNDS: LogViewBounds = {maxLines: 5000, maxLineChars: 4000};

export interface LogView {
  readonly bounds: LogViewBounds;
  /** The complete lines kept, oldest first. */
  readonly lines: readonly string[];
  /** The unfinished last line; `''` when the text so far ends with a line end. */
  readonly partial: string;
  /** Complete lines evicted from the front by `maxLines`: `lines[0]` is line `evictedLines`. */
  readonly evictedLines: number;
  /** The byte offset the view starts at; null before the first chunk. Above 0, earlier output exists. */
  readonly start: number | null;
  /** The byte offset the next chunk must start at; null before the first chunk. */
  readonly nextOffset: number | null;
  /** Bytes the store evicted after the view started, which the view therefore never showed. */
  readonly skippedBytes: number;
  /** The first chunk started past bytes the store evicted. */
  readonly truncatedBefore: boolean;
  /** The last chunk reached the end of what the stream held then. */
  readonly eof: boolean;
  /** The stream is closed: once `eof`, the view is complete. */
  readonly closed: boolean;
}

export function emptyLogView(bounds: LogViewBounds = DEFAULT_LOG_VIEW_BOUNDS): LogView {
  return {
    bounds,
    lines: [],
    partial: '',
    evictedLines: 0,
    start: null,
    nextOffset: null,
    skippedBytes: 0,
    truncatedBefore: false,
    eof: false,
    closed: false,
  };
}

/** Whether the view has every byte the stream will ever have. */
export function logViewComplete(view: LogView): boolean {
  return view.closed && view.eof;
}

/** The lines to render: the kept complete lines, then the unfinished one when there is one. */
export function logViewLines(view: LogView): readonly string[] {
  return view.partial === '' ? view.lines : [...view.lines, view.partial];
}

function wrap(line: string, max: number): string[] {
  if (line.length <= max) return [line];
  const pieces: string[] = [];
  for (let index = 0; index < line.length; index += max)
    pieces.push(line.slice(index, index + max));
  return pieces;
}

/** Fold one chunk into the view. A stale chunk (it starts before `nextOffset`) leaves it unchanged. */
export function foldChunk(view: LogView, chunk: LogChunk): LogView {
  const expected = view.nextOffset;
  if (expected !== null && chunk.offset < expected) return view;
  const gap = expected === null ? 0 : chunk.offset - expected;
  const {maxLines, maxLineChars} = view.bounds;
  const pieces = (gap > 0 && view.partial !== '' ? `${view.partial}\n` : view.partial)
    .concat(chunk.data)
    .split('\n');
  let partial = pieces.pop() ?? '';
  const added = pieces.flatMap(line => wrap(line, maxLineChars));
  while (partial.length > maxLineChars) {
    added.push(partial.slice(0, maxLineChars));
    partial = partial.slice(maxLineChars);
  }
  const all = added.length === 0 ? view.lines : [...view.lines, ...added];
  const overflow = Math.max(all.length - maxLines, 0);
  return {
    bounds: view.bounds,
    lines: overflow === 0 ? all : all.slice(overflow),
    partial,
    evictedLines: view.evictedLines + overflow,
    start: view.start ?? chunk.offset,
    nextOffset: chunk.next_offset,
    skippedBytes: view.skippedBytes + gap,
    truncatedBefore: view.truncatedBefore || (expected === null && chunk.truncated_before),
    eof: chunk.eof,
    closed: chunk.closed,
  };
}
