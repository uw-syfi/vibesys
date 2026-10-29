import {describe, expect, test} from 'bun:test';
import type {LogChunk} from '@vibesys/contracts/host-helper';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {emptyLogView, foldChunk, logViewLines, stripEscapes} from './index.js';

const encoder = new TextEncoder();
const ESC = '\u001b';
const BEL = '\u0007';
/** Printable text, line ends and a bell outside any sequence, which is text like any other. */
const PRINTABLE = ['a', 'Z', '0', ' ', '[', ']', 'm', ';', '\\', 'é', '€', '\t', BEL, '\n'];

function pickText(random: Seeded, length: number): string {
  return Array.from({length}, () => random.pick(PRINTABLE)).join('');
}

/** One well-formed escape sequence of a kind a tool writes. */
function sequence(random: Seeded): string {
  const params = Array.from({length: random.int(4)}, () => random.pick(['0', '1', '33', ';', '?']));
  switch (random.int(4)) {
    case 0:
      return `${ESC}[${params.join('')}${random.pick(['m', 'K', 'H', 'J', 'A', '~'])}`;
    case 1:
      return `${ESC}]0;${random.pick(['title', 'build', ''])}${random.pick([BEL, `${ESC}\\`])}`;
    case 2:
      return `${ESC}${random.pick(['(', ')', ''])}${random.pick(['B', '0', '=', 'M'])}`;
    default:
      return `${ESC}[${params.join('')}m`;
  }
}

/** Text mixed with sequences, and the text alone: what stripping must leave. */
function generated(random: Seeded): {readonly raw: string; readonly text: string} {
  let raw = '';
  let text = '';
  for (let part = 0; part < random.int(10); part += 1) {
    if (random.bool()) {
      raw += sequence(random);
    } else {
      const piece = pickText(random, random.int(6));
      raw += piece;
      text += piece;
    }
  }
  return {raw, text};
}

function chunked(random: Seeded, whole: string): LogChunk[] {
  const chunks: LogChunk[] = [];
  let offset = 0;
  let index = 0;
  while (index < whole.length || chunks.length === 0) {
    const take = Math.min(whole.length - index, random.int(6));
    const data = whole.slice(index, index + take);
    const next = offset + encoder.encode(data).length;
    index += take;
    chunks.push({
      offset,
      data,
      next_offset: next,
      eof: index >= whole.length,
      closed: false,
      truncated_before: false,
      pipes: next === offset ? [] : [{offset, length: next - offset, pipe: 'stdout'}],
    });
    offset = next;
  }
  return chunks;
}

describe('escape sequences', () => {
  test('property: stripping keeps exactly the text outside the sequences, and is idempotent', () => {
    for (const seed of SEEDS) {
      const {raw, text} = generated(new Seeded(seed));
      expect(stripEscapes(raw), `seed ${seed}`).toBe(text);
      expect(stripEscapes(stripEscapes(raw)), `seed ${seed}`).toBe(stripEscapes(raw));
    }
  });

  test('property: stripping never removes a character of text without an escape in it', () => {
    for (const seed of SEEDS) {
      const text = pickText(new Seeded(seed), 40);
      expect(stripEscapes(text)).toBe(text);
    }
  });

  test('property: the fold drops them however the stream was chunked', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      const {raw, text} = generated(random);
      const view = chunked(random, raw).reduce(foldChunk, emptyLogView());
      // The same lines as the text alone, written with no escapes at all.
      const plain = chunked(random, text).reduce(foldChunk, emptyLogView());
      expect(logViewLines(view), `seed ${seed}`).toEqual(logViewLines(plain));
    }
  });

  test('a docker warning reads as its words', () => {
    expect(stripEscapes(`${ESC}[33m1 warning found${ESC}[0m (use docker --debug)`)).toBe(
      '1 warning found (use docker --debug)',
    );
  });

  test('a character a sequence cannot hold ends it and stays, as does a line end', () => {
    expect(stripEscapes(`${ESC}[31éx`)).toBe('éx');
    expect(stripEscapes(`${ESC}]title\nnext`)).toBe('\nnext');
  });
});
