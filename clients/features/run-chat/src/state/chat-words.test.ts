import {describe, expect, test} from 'bun:test';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {
  type ErrorReport,
  errorQuestion,
  failedAnswerReport,
  STEER_MODES,
  type SteerMode,
  type SteerTarget,
  steerFailureReport,
  steerHelp,
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

  test('a long where is cut to 256 bytes, on a code point, with an ellipsis', () => {
    for (const character of ['a', 'é', '中', '😀']) {
      const where = character.repeat(400);
      const question = errorQuestion({where, message: 'No', detail: null}, 65_536);
      const shown = question.slice(question.indexOf('(') + 1, question.indexOf('):'));
      expect(shown.endsWith('…')).toBe(true);
      const kept = shown.slice(0, -1);
      expect(where.startsWith(kept)).toBe(true);
      expect(bytes(kept)).toBeLessThanOrEqual(256);
      expect(bytes(kept) + bytes(character)).toBeGreaterThan(256);
    }
    const short = 'x'.repeat(256);
    expect(errorQuestion({where: short, message: 'No', detail: null}, 65_536)).toContain(
      `(${short}):`,
    );
  });

  test('a cap too small for the question itself is refused', () => {
    expect(() => errorQuestion({where: 'x', message: 'y', detail: null}, 1000)).toThrow(RangeError);
  });
});

test('each chat failure says where it happened', () => {
  const failure = {message: 'Refused', detail: 'trace'};
  expect(steerFailureReport(failure)).toEqual({where: 'Steer', ...failure});
  expect(failedAnswerReport('quota exhausted')).toEqual({
    where: 'Run chat answer',
    message: 'quota exhausted',
    detail: null,
  });
});

describe('a steer card says what its When and To choices do', () => {
  const TARGETS: readonly SteerTarget[] = [
    {kind: 'run', id: null},
    {kind: 'role', id: 'implementer'},
    {kind: 'attempt', id: 'w2-a1'},
    {kind: 'item', id: 'w4'},
  ];
  const CASES = TARGETS.flatMap(target => STEER_MODES.map(({value: mode}) => ({mode, target})));

  test('in one sentence for every choice, each different', () => {
    const lines = CASES.map(steerHelp);
    for (const line of lines) expect(line).toMatch(/^[A-Z][^.]*\.$/);
    expect(new Set(lines).size).toBe(CASES.length);
  });

  /** The words that name whom a choice reaches. */
  const WHOLE_RUN: Record<SteerMode, string> = {
    next_turn: 'the first agent to start a turn',
    standing: 'every agent',
    interrupt: 'one agent',
  };
  function reaches(mode: SteerMode, target: SteerTarget): string {
    if (target.kind === 'run') return WHOLE_RUN[mode];
    return target.kind === 'role' ? 'the Implementer' : `${target.kind} ${target.id}`;
  }

  test('naming whom it reaches: one first agent, every agent, or the target by name', () => {
    for (const {mode, target} of CASES)
      expect(steerHelp({mode, target})).toContain(reaches(mode, target));
  });

  test('every turn ends when withdrawn or when its target or the run ends, and offers no control', () => {
    for (const {mode, target} of CASES) {
      const line = steerHelp({mode, target});
      expect(line).not.toContain('you withdraw');
      if (mode !== 'standing') continue;
      expect(line).toContain('until it is withdrawn or');
      const ends = target.kind === 'attempt' || target.kind === 'item' ? target.kind : 'run';
      expect(line).toContain(`the ${ends} ends`);
    }
  });
});
