/**
 * `FakeLogStore`: the run log streams a `FakeHelper` serves through `logs.list` and `logs.read`,
 * held to the helper's chunk rules (`docs/contributing/desktop-connection.md`, "Logs"): a chunk
 * that more bytes follow ends after its window's last line end, or before a character the window
 * splits; a tail that does not start the stream starts at its first line start; an open stream's
 * unfinished last character is held back; bytes that are not UTF-8 decode as U+FFFD. The
 * `helper-logs-*` scenarios replay against it unchanged, so it cuts exactly as the real helper does.
 *
 * It never waits: a read at the end of an open stream is answered at once, as the real helper
 * answers when its wait ends with nothing new.
 */
import type {LogChunk, LogStream} from '@vibesys/contracts/host-helper';

type Pipe = 'stdout' | 'stderr';
type Outcome = 'ended' | 'failed' | 'abandoned';
type Span = {readonly offset: number; readonly length: number; readonly pipe: Pipe};

interface StoredStream {
  readonly owner: {readonly [key: string]: unknown};
  readonly command: string;
  held: Uint8Array;
  oldest: number;
  spans: Span[];
  outcome: Outcome | null;
}

/** What `fixtures/helper-log-store.json` (the scenarios' store) holds. */
export interface LogStoreFixture {
  readonly instances: Readonly<
    Record<
      string,
      readonly {
        readonly id: string;
        readonly owner: {readonly [key: string]: unknown};
        readonly command: string;
        readonly writes: readonly {readonly pipe: Pipe; readonly text: string}[];
        readonly evict_before: number;
        readonly outcome: Outcome | null;
      }[]
    >
  >;
}

export type LogStart = {readonly offset: number} | {readonly tail_bytes: number};

export class UnknownLogStream extends Error {}

const NEWLINE = 0x0a;
const encoder = new TextEncoder();
const decoder = new TextDecoder('utf-8', {fatal: false, ignoreBOM: true});

function isContinuation(byte: number): boolean {
  return (byte & 0b1100_0000) === 0b1000_0000;
}

function characterLength(lead: number): number {
  if (lead >= 0b1111_0000) return 4;
  if (lead >= 0b1110_0000) return 3;
  if (lead >= 0b1100_0000) return 2;
  return 1;
}

/** The length of `data` without a final character its last bytes only begin. */
function completePrefix(data: Uint8Array): number {
  for (let back = 1; back <= Math.min(data.length, 3); back += 1) {
    const byte = data[data.length - back] as number;
    if (!isContinuation(byte))
      return characterLength(byte) > back ? data.length - back : data.length;
  }
  return data.length;
}

/** Where the first whole line in `data` begins, or its first character when none does. */
function lineStart(data: Uint8Array): number {
  const newline = data.indexOf(NEWLINE);
  if (newline >= 0 && newline < data.length - 1) return newline + 1;
  let skip = 0;
  while (skip < Math.min(data.length, 3) && isContinuation(data[skip] as number)) skip += 1;
  return skip;
}

function clip(spans: readonly Span[], start: number, end: number): Span[] {
  const clipped: Span[] = [];
  for (const span of spans) {
    const low = Math.max(span.offset, start);
    const high = Math.min(span.offset + span.length, end);
    if (high > low) clipped.push({offset: low, length: high - low, pipe: span.pipe});
  }
  return clipped;
}

export class FakeLogStore {
  readonly #instances = new Map<string, Map<string, StoredStream>>();

  /** The store `fixture` describes. */
  static from(fixture: LogStoreFixture): FakeLogStore {
    const store = new FakeLogStore();
    for (const [instance, streams] of Object.entries(fixture.instances)) {
      for (const stream of streams) {
        store.open(instance, stream.id, stream.owner, stream.command);
        for (const write of stream.writes) {
          store.write(instance, stream.id, encoder.encode(write.text), write.pipe);
        }
        store.evict(instance, stream.id, stream.evict_before);
        if (stream.outcome !== null) store.close(instance, stream.id, stream.outcome);
      }
    }
    return store;
  }

  has(instance: string): boolean {
    return this.#instances.has(instance);
  }

  open(
    instance: string,
    id: string,
    owner: {readonly [key: string]: unknown},
    command: string,
  ): void {
    const streams = this.#instances.get(instance) ?? new Map<string, StoredStream>();
    streams.set(id, {owner, command, held: new Uint8Array(), oldest: 0, spans: [], outcome: null});
    this.#instances.set(instance, streams);
  }

  write(instance: string, id: string, data: Uint8Array, pipe: Pipe = 'stdout'): void {
    const stream = this.#stream(instance, id);
    if (data.length === 0) return;
    const end = stream.oldest + stream.held.length;
    const last = stream.spans.at(-1);
    if (last !== undefined && last.pipe === pipe) {
      stream.spans[stream.spans.length - 1] = {...last, length: last.length + data.length};
    } else {
      stream.spans.push({offset: end, length: data.length, pipe});
    }
    const held = new Uint8Array(stream.held.length + data.length);
    held.set(stream.held);
    held.set(data, stream.held.length);
    stream.held = held;
  }

  close(instance: string, id: string, outcome: Outcome = 'ended'): void {
    this.#stream(instance, id).outcome = outcome;
  }

  evict(instance: string, id: string, before: number): void {
    const stream = this.#stream(instance, id);
    const end = stream.oldest + stream.held.length;
    const drop = Math.max(0, Math.min(before, end) - stream.oldest);
    stream.held = stream.held.slice(drop);
    stream.oldest += drop;
    stream.spans = clip(stream.spans, stream.oldest, end);
  }

  /** The instance's streams, oldest first; `[]` when it has none. */
  streams(instance: string): LogStream[] {
    return [...(this.#instances.get(instance) ?? new Map<string, StoredStream>())].map(
      ([id, stream]) => ({
        id,
        owner: stream.owner as LogStream['owner'],
        command: stream.command,
        retained_start: stream.oldest,
        size: stream.oldest + stream.held.length,
        outcome: stream.outcome,
      }),
    );
  }

  /** One chunk, cut as the helper cuts it; `UnknownLogStream` when there is no such stream. */
  read(instance: string, id: string, start: LogStart, maxBytes: number): LogChunk {
    const stream = this.#instances.get(instance)?.get(id);
    if (stream === undefined) throw new UnknownLogStream(id);
    const end = stream.oldest + stream.held.length;
    const tail = 'tail_bytes' in start;
    const wanted = tail ? Math.max(end - Math.min(start.tail_bytes, maxBytes), 0) : start.offset;
    let offset = Math.max(wanted, stream.oldest);
    let data = stream.held.slice(offset - stream.oldest, offset - stream.oldest + maxBytes);
    const more = offset + data.length < end;
    if (tail && offset > 0) {
      const skip = lineStart(data);
      data = data.slice(skip);
      offset += skip;
    }
    if (more && data.includes(NEWLINE)) data = data.slice(0, data.lastIndexOf(NEWLINE) + 1);
    else if (more || stream.outcome === null) data = data.slice(0, completePrefix(data));
    return {
      offset,
      data: decoder.decode(data),
      next_offset: offset + data.length,
      eof: !more,
      closed: stream.outcome !== null,
      truncated_before: wanted < stream.oldest,
      pipes: clip(stream.spans, offset, offset + data.length),
    };
  }

  #stream(instance: string, id: string): StoredStream {
    const stream = this.#instances.get(instance)?.get(id);
    if (stream === undefined) throw new UnknownLogStream(id);
    return stream;
  }
}
