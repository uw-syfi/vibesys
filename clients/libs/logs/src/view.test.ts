import {describe, expect, test} from 'bun:test';
import type {LogChunk, LogPipe} from '@vibesys/contracts/host-helper';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {
  emptyLogView,
  foldChunk,
  type LogView,
  type LogViewBounds,
  logViewLines,
  logViewRecords,
  pipeSegments,
} from './index.js';

const encoder = new TextEncoder();
const ALPHABET = ['a', 'b', '\n', 'é', '€', ' ', '\n'];

function text(random: Seeded, length: number): string {
  return Array.from({length}, () => random.pick(ALPHABET)).join('');
}

/** `whole` cut into chunks at random character boundaries, with byte offsets from `start`. */
function chunked(random: Seeded, whole: string, start = 0, closed = true): LogChunk[] {
  const chunks: LogChunk[] = [];
  let offset = start;
  let index = 0;
  while (index < whole.length || chunks.length === 0) {
    const take = Math.min(whole.length - index, random.int(12));
    const data = whole.slice(index, index + take);
    const next = offset + encoder.encode(data).length;
    index += take;
    const last = index >= whole.length;
    chunks.push({
      offset,
      data,
      next_offset: next,
      eof: last,
      closed: last && closed,
      truncated_before: false,
      pipes: next === offset ? [] : [{offset, length: next - offset, pipe: 'stdout'}],
    });
    offset = next;
  }
  return chunks;
}

function fold(chunks: readonly LogChunk[], bounds: LogViewBounds): LogView {
  return chunks.reduce(foldChunk, emptyLogView(bounds));
}

/** What a view of `whole` holds with no line or memory bound: its lines, wrapped. */
function expectedLines(whole: string, maxLineChars: number): string[] {
  const lines: string[] = [];
  for (const line of whole.split('\n')) {
    if (line.length === 0) lines.push('');
    for (let index = 0; index < line.length; index += maxLineChars)
      lines.push(line.slice(index, index + maxLineChars));
  }
  return lines;
}

describe('foldChunk', () => {
  test('property: any chunking of the same text gives the same lines', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      const whole = text(random, random.int(120));
      const bounds = {maxLines: 1000, maxLineChars: 1 + random.int(10)};
      const one = fold(chunked(new Seeded(seed + 1), whole), bounds);
      const other = fold(chunked(new Seeded(seed + 2), whole), bounds);
      expect({seed, lines: logViewLines(one)}).toEqual({seed, lines: logViewLines(other)});
      expect(one.nextOffset).toBe(encoder.encode(whole).length);
      expect(one.closed && one.eof).toBe(true);
      const all = expectedLines(whole, bounds.maxLineChars);
      const rendered = whole.endsWith('\n') || whole === '' ? all.slice(0, -1) : all;
      expect({seed, lines: logViewLines(one)}).toEqual({seed, lines: rendered});
    }
  });

  test('property: the bound holds and evicted lines are counted', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      const whole = text(random, random.int(200));
      const bounds = {maxLines: 1 + random.int(6), maxLineChars: 1 + random.int(8)};
      const unbounded = fold(chunked(new Seeded(seed + 1), whole), {...bounds, maxLines: 100_000});
      let view = emptyLogView(bounds);
      for (const chunk of chunked(new Seeded(seed + 1), whole)) {
        view = foldChunk(view, chunk);
        expect(view.lines.length).toBeLessThanOrEqual(bounds.maxLines);
        expect(view.partial.text.length).toBeLessThanOrEqual(bounds.maxLineChars);
      }
      expect(view.evictedLines + view.lines.length).toBe(unbounded.lines.length);
      expect(view.lines).toEqual(unbounded.lines.slice(view.evictedLines));
      expect(view.partial).toEqual(unbounded.partial);
    }
  });

  test('a stale chunk changes nothing', () => {
    const [first, second] = chunked(new Seeded(1), 'one\ntwo\nthree\n'.repeat(3));
    if (first === undefined || second === undefined) throw new Error('two chunks expected');
    const view = foldChunk(foldChunk(emptyLogView(), first), second);
    expect(foldChunk(view, first)).toBe(view);
  });

  test('a gap ends the unfinished line and counts the skipped bytes', () => {
    const bounds = {maxLines: 10, maxLineChars: 100};
    let view = foldChunk(emptyLogView(bounds), {
      offset: 0,
      data: 'abc',
      next_offset: 3,
      eof: true,
      closed: false,
      truncated_before: false,
      pipes: [{offset: 0, length: 3, pipe: 'stdout'}],
    });
    view = foldChunk(view, {
      offset: 10,
      data: 'xyz\n',
      next_offset: 14,
      eof: true,
      closed: true,
      truncated_before: true,
      pipes: [{offset: 10, length: 4, pipe: 'stdout'}],
    });
    expect(logViewLines(view)).toEqual(['abc', 'xyz']);
    expect(view.skippedBytes).toBe(7);
    expect(view.truncatedBefore).toBe(false);
  });

  test('a tail that starts past evicted bytes says so, and where the view starts', () => {
    const view = foldChunk(emptyLogView(), {
      offset: 40,
      data: 'last\n',
      next_offset: 45,
      eof: true,
      closed: false,
      truncated_before: true,
      pipes: [{offset: 40, length: 5, pipe: 'stdout'}],
    });
    expect([view.start, view.truncatedBefore, logViewLines(view)]).toEqual([40, true, ['last']]);
  });

  test('property: each character keeps the pipe that wrote it, however the stream was chunked', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      const written = piped(random, random.int(150));
      const bounds = {maxLines: 1000, maxLineChars: 1 + random.int(10)};
      const one = fold(pipedChunks(new Seeded(seed + 1), written), bounds);
      const other = fold(pipedChunks(new Seeded(seed + 2), written), bounds);
      expect({seed, lines: logViewRecords(one)}).toEqual({seed, lines: logViewRecords(other)});
      const shown = logViewRecords(one).flatMap(line => {
        let at = 0;
        for (const run of line.runs) {
          expect(run.length).toBeGreaterThan(0);
          at += run.length;
        }
        expect(at).toBe(line.text.length);
        line.runs.forEach((run, index) => {
          if (index > 0) expect(run.pipe).not.toBe(line.runs[index - 1]?.pipe);
        });
        return pipeSegments(line).flatMap(segment =>
          segment.text.split('').map(unit => ({unit, pipe: segment.pipe})),
        );
      });
      // Wrapping counts UTF-16 units, so compare unit by unit.
      const expected = written
        .filter(entry => entry.character !== '\n')
        .flatMap(entry => entry.character.split('').map(unit => ({unit, pipe: entry.pipe})));
      expect({seed, shown}).toEqual({seed, shown: expected});
    }
  });

  test('a gap keeps the pipe of the line it ends', () => {
    let view = foldChunk(emptyLogView(), {
      offset: 0,
      data: 'oops',
      next_offset: 4,
      eof: true,
      closed: false,
      truncated_before: false,
      pipes: [{offset: 0, length: 4, pipe: 'stderr'}],
    });
    view = foldChunk(view, {
      offset: 9,
      data: 'ok\n',
      next_offset: 12,
      eof: true,
      closed: false,
      truncated_before: false,
      pipes: [{offset: 9, length: 3, pipe: 'stdout'}],
    });
    expect(logViewRecords(view)).toEqual([
      {text: 'oops', runs: [{length: 4, pipe: 'stderr'}]},
      {text: 'ok', runs: [{length: 2, pipe: 'stdout'}]},
    ]);
  });
});

interface Written {
  readonly character: string;
  readonly pipe: LogPipe;
}

/** Text whose characters each pipe wrote in random stretches. */
function piped(random: Seeded, length: number): Written[] {
  const written: Written[] = [];
  let pipe: LogPipe = 'stdout';
  for (let index = 0; index < length; index++) {
    if (random.int(4) === 0) pipe = pipe === 'stdout' ? 'stderr' : 'stdout';
    written.push({character: random.pick([...ALPHABET, '\u{1F600}']), pipe});
  }
  return written;
}

/** `written` cut into chunks at random character boundaries, each with its exact pipe spans. */
function pipedChunks(random: Seeded, written: readonly Written[]): LogChunk[] {
  const chunks: LogChunk[] = [];
  let offset = 0;
  let index = 0;
  while (index < written.length || chunks.length === 0) {
    const take = written.slice(index, index + random.int(12));
    index += take.length;
    const pipes: {offset: number; length: number; pipe: LogPipe}[] = [];
    let at = offset;
    for (const entry of take) {
      const length = encoder.encode(entry.character).length;
      const last = pipes.at(-1);
      if (last !== undefined && last.pipe === entry.pipe) last.length += length;
      else pipes.push({offset: at, length, pipe: entry.pipe});
      at += length;
    }
    const last = index >= written.length;
    chunks.push({
      offset,
      data: take.map(entry => entry.character).join(''),
      next_offset: at,
      eof: last,
      closed: last,
      truncated_before: false,
      pipes,
    });
    offset = at;
  }
  return chunks;
}
