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
 *
 * Each line keeps which pipe wrote which of its characters (`runs`, from the chunk's `pipes`
 * spans), so a view can mark stderr. Spans count bytes and the text counts characters: a character
 * takes the pipe of the span holding its first byte and covers its UTF-8 length. A U+FFFD that
 * replaced bytes that were not UTF-8 counts as 3 bytes, so after such bytes the runs are only as
 * exact as the decoded text allows.
 *
 * Terminal escape sequences (colors, cursor moves, titles) are dropped as the text is folded
 * (`escapes.ts`), so the lines are text only. A sequence a chunk cut is finished by the next chunk:
 * the view carries the scanner's state (`escape`), and a gap resets it with the line it ends.
 */
import type {LogChunk, LogPipe} from '@vibesys/contracts/host-helper';
import {type EscapeState, escapeStep} from './escapes.js';

export interface LogViewBounds {
  /** The most complete lines the view keeps. */
  readonly maxLines: number;
  /** The longest line; a longer one is wrapped into pieces of this many UTF-16 units. */
  readonly maxLineChars: number;
}

export const DEFAULT_LOG_VIEW_BOUNDS: LogViewBounds = {maxLines: 5000, maxLineChars: 4000};

/** `length` UTF-16 units of a line, written by `pipe`. */
export interface PipeRun {
  readonly length: number;
  readonly pipe: LogPipe;
}

/** One line of a stream: its text, and which pipe wrote it. */
export interface LogLine {
  readonly text: string;
  /** Runs covering `text` in order; adjacent runs differ in pipe and none is empty. */
  readonly runs: readonly PipeRun[];
}

const EMPTY_LINE: LogLine = {text: '', runs: []};

export interface LogView {
  readonly bounds: LogViewBounds;
  /** The complete lines kept, oldest first. */
  readonly lines: readonly LogLine[];
  /** The unfinished last line; empty when the text so far ends with a line end. */
  readonly partial: LogLine;
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
  /** Where the escape scanner stopped: `text`, or inside a sequence the next chunk finishes. */
  readonly escape: EscapeState;
}

export function emptyLogView(bounds: LogViewBounds = DEFAULT_LOG_VIEW_BOUNDS): LogView {
  return {
    bounds,
    lines: [],
    partial: EMPTY_LINE,
    evictedLines: 0,
    start: null,
    nextOffset: null,
    skippedBytes: 0,
    truncatedBefore: false,
    eof: false,
    closed: false,
    escape: 'text',
  };
}

/** Whether the view has every byte the stream will ever have. */
export function logViewComplete(view: LogView): boolean {
  return view.closed && view.eof;
}

/** The lines to render: the kept complete lines, then the unfinished one when there is one. */
export function logViewRecords(view: LogView): readonly LogLine[] {
  return view.partial.text === '' ? view.lines : [...view.lines, view.partial];
}

/** The text of the lines `logViewRecords` renders. */
export function logViewLines(view: LogView): readonly string[] {
  return logViewRecords(view).map(line => line.text);
}

/** A piece of a line one pipe wrote, starting `at` UTF-16 units into the line. */
export interface PipeSegment {
  readonly at: number;
  readonly text: string;
  readonly pipe: LogPipe;
}

/** The pieces of `line` each pipe wrote, in order. */
export function pipeSegments(line: LogLine): PipeSegment[] {
  let at = 0;
  return line.runs.map(run => {
    const segment = {at, text: line.text.slice(at, at + run.length), pipe: run.pipe};
    at += run.length;
    return segment;
  });
}

function pushRun(runs: PipeRun[], length: number, pipe: LogPipe): void {
  if (length === 0) return;
  const last = runs.at(-1);
  if (last !== undefined && last.pipe === pipe)
    runs[runs.length - 1] = {length: last.length + length, pipe};
  else runs.push({length, pipe});
}

function joined(line: LogLine, text: string, runs: readonly PipeRun[]): LogLine {
  if (text === '') return line;
  const all = [...line.runs];
  for (const run of runs) pushRun(all, run.length, run.pipe);
  return {text: line.text + text, runs: all};
}

/** The units `[from, to)` of `line`. */
function slice(line: LogLine, from: number, to: number): LogLine {
  const runs: PipeRun[] = [];
  let at = 0;
  for (const run of line.runs) {
    const start = Math.max(at, from);
    const end = Math.min(at + run.length, to);
    if (end > start) pushRun(runs, end - start, run.pipe);
    at += run.length;
  }
  return {text: line.text.slice(from, to), runs};
}

function wrap(line: LogLine, max: number): LogLine[] {
  if (line.text.length <= max) return [line];
  const pieces: LogLine[] = [];
  for (let index = 0; index < line.text.length; index += max)
    pieces.push(slice(line, index, index + max));
  return pieces;
}

function utf8Length(codePoint: number): number {
  if (codePoint < 0x80) return 1;
  if (codePoint < 0x800) return 2;
  return codePoint < 0x10000 ? 3 : 4;
}

/**
 * The chunk's text as lines, without escape sequences: those a line end finished, then what
 * follows the last line end, and where the escape scanner stopped (starting from `from`).
 */
function chunkLines(
  chunk: LogChunk,
  from: EscapeState,
): {readonly ended: LogLine[]; readonly rest: LogLine; readonly escape: EscapeState} {
  const spans = chunk.pipes;
  const ended: LogLine[] = [];
  let span = 0;
  let byte = chunk.offset;
  let text = '';
  let runs: PipeRun[] = [];
  let state = from;
  for (const character of chunk.data) {
    while (span < spans.length - 1) {
      const current = spans[span];
      if (current === undefined || byte < current.offset + current.length) break;
      span += 1;
    }
    const pipe = spans[span]?.pipe ?? 'stdout';
    byte += utf8Length(character.codePointAt(0) ?? 0);
    const step = escapeStep(state, character);
    state = step.next;
    if (!step.keep) continue;
    if (character === '\n') {
      ended.push({text, runs});
      text = '';
      runs = [];
      continue;
    }
    text += character;
    pushRun(runs, character.length, pipe);
  }
  return {ended, rest: {text, runs}, escape: state};
}

/** Fold one chunk into the view. A stale chunk (it starts before `nextOffset`) leaves it unchanged. */
export function foldChunk(view: LogView, chunk: LogChunk): LogView {
  const expected = view.nextOffset;
  if (expected !== null && chunk.offset < expected) return view;
  const gap = expected === null ? 0 : chunk.offset - expected;
  const {maxLines, maxLineChars} = view.bounds;
  // A gap ends the unfinished line, and with it any sequence the scanner was inside.
  const lines = chunkLines(chunk, gap > 0 ? 'text' : view.escape);
  const {ended, rest} = lines;
  // After a gap the unfinished line ends where the view ended; otherwise the chunk continues it.
  const carried = gap > 0 && view.partial.text !== '' ? [view.partial] : [];
  const continued = carried.length > 0 ? EMPTY_LINE : view.partial;
  const [first, ...others] = ended;
  const finished =
    first === undefined ? [] : [joined(continued, first.text, first.runs), ...others];
  const added = [...carried, ...finished].flatMap(line => wrap(line, maxLineChars));
  let partial = first === undefined ? joined(continued, rest.text, rest.runs) : rest;
  while (partial.text.length > maxLineChars) {
    added.push(slice(partial, 0, maxLineChars));
    partial = slice(partial, maxLineChars, partial.text.length);
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
    escape: lines.escape,
  };
}
