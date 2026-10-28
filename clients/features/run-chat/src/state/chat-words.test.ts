import {describe, expect, test} from 'bun:test';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {
  chatFailureReport,
  type ErrorReport,
  errorQuestion,
  failedAnswerReport,
  steerFailureReport,
} from './index.js';

const encoder = new TextEncoder();
const bytes = (text: string): number => encoder.encode(text).length;
const MARK = '[earlier output cut]';
const CLOSING = "\n\nPlease look through this run's logs";

/** ASCII, then characters of two, three and four UTF-8 bytes. */
const CHARACTERS = ['a', 'é', '中', '😀', '\n'];

/** A string of `length` code points, in runs of one character so long ones stay cheap to build. */
function text(random: Seeded, length: number): string {
  let out = '';
  for (let left = length; left > 0; ) {
    const run = Math.min(left, 1 + random.int(500));
    out += random.pick(CHARACTERS).repeat(run);
    left -= run;
  }
  return out;
}

function report(random: Seeded): ErrorReport {
  return {
    where: text(random, random.int(40)),
    message: text(random, random.int(40_000)),
    detail: random.bool() ? null : text(random, random.int(40_000)),
  };
}

describe('errorQuestion', () => {
  test("property: a question never exceeds the cap and keeps the diagnostic's end", () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      const asked = report(random);
      const maxBytes = 1024 + random.int(70_000 - 1024 + 1);
      const question = errorQuestion(asked, maxBytes);
      const whole = errorQuestion(asked, Number.POSITIVE_INFINITY);
      expect(bytes(question)).toBeLessThanOrEqual(maxBytes);
      expect(question).toContain(asked.where);
      // No code point is split: the bytes decode strictly back to the same text.
      expect(new TextDecoder('utf-8', {fatal: true}).decode(encoder.encode(question))).toBe(
        question,
      );
      if (bytes(whole) <= maxBytes) expect(question).toBe(whole);
      else if (question.includes(MARK)) {
        const start = question.indexOf(`${MARK}\n`) + MARK.length + 1;
        const kept = question.slice(start, question.lastIndexOf(CLOSING));
        expect(asked.detail?.endsWith(kept)).toBe(true);
      }
    }
  });

  test('a diagnostic over the cap keeps its tail, after a marker', () => {
    const detail = `${'x'.repeat(69_990)}last words`;
    const question = errorQuestion({where: 'Run ended', message: 'Failed', detail}, 65_536);
    expect(bytes(question)).toBeLessThanOrEqual(65_536);
    expect(bytes(question)).toBeGreaterThan(65_000);
    expect(question).toContain(`Details:\n${MARK}\n`);
    expect(question).toContain('last words\n\nPlease look through');
    expect(question).toContain('Failed');
  });

  test('a report without a diagnostic asks without a Details block', () => {
    const question = errorQuestion({where: 'Steer', message: 'No', detail: null}, 65_536);
    expect(question).toBe(
      [
        'I got this error in the VibeSys app (Steer):',
        '',
        'No',
        '',
        "Please look through this run's logs, find what caused it, and tell me what to do about it.",
      ].join('\n'),
    );
  });

  test('a cap too small for the question itself is refused', () => {
    expect(() => errorQuestion({where: 'x', message: 'y', detail: null}, 1000)).toThrow(RangeError);
  });
});

test('each chat failure says where it happened', () => {
  const failure = {message: 'Refused', detail: 'trace'};
  expect(chatFailureReport(failure)).toEqual({where: 'Run chat', ...failure});
  expect(steerFailureReport(failure)).toEqual({where: 'Steer', ...failure});
  expect(failedAnswerReport('quota exhausted')).toEqual({
    where: 'Run chat answer',
    message: 'quota exhausted',
    detail: null,
  });
});
