import {expect, test} from 'bun:test';
import {AbsenceReason} from '@vibesys/proto';
import {
  CONNECT_STEPS,
  type FailureReason,
  RETRYABLE_REASONS,
  type StaleReason,
  type SubscriptionState,
  TERMINAL_REASONS,
} from '@vibesys/subscription';
import {machineEntry} from '@vibesys/subscription/testing';
import fc from 'fast-check';
import {ageWord, FAILURE_WORDS, statsGauges, statsView} from './index.js';

const GIB = 2 ** 30;

/** The stats of a sample, as the topic's value (the entry's message). */
function sample(readings: Parameters<typeof machineEntry>[2]) {
  const {value} = machineEntry('machine', 1_000, readings);
  if (value.case !== 'machineStats') throw new Error('not stats');
  return value.value;
}

const STALE_REASON = fc.constantFrom<StaleReason>(...RETRYABLE_REASONS);
const FAILURE = fc.constantFrom<FailureReason>(...RETRYABLE_REASONS, ...TERMINAL_REASONS);
const STATE: fc.Arbitrary<SubscriptionState<ReturnType<typeof sample>>> = fc.oneof(
  fc.record({
    kind: fc.constant('connecting' as const),
    step: fc.constantFrom(...CONNECT_STEPS),
    attempt: fc.nat({max: 9}),
  }),
  fc.record({
    kind: fc.constant('live' as const),
    value: fc.constant(sample({})),
    since: fc.nat(),
  }),
  fc.record({
    kind: fc.constant('stale' as const),
    value: fc.constant(sample({})),
    reason: STALE_REASON,
    attempt: fc.integer({min: 1, max: 4}),
    valueAt: fc.nat(),
  }),
  fc.record({kind: fc.constant('failed' as const), reason: FAILURE, retrying: fc.boolean()}),
);

test('property: a value is shown exactly while the subscription holds one, and stale only when it is', () => {
  fc.assert(
    fc.property(STATE, state => {
      const view = statsView(state);
      expect(view.kind === 'values').toBe(state.kind === 'live' || state.kind === 'stale');
      if (view.kind === 'values') {
        expect(view.stale === null).toBe(state.kind === 'live');
        if (state.kind === 'stale') expect(view.stale?.valueAt).toBe(state.valueAt);
      }
      if (view.kind === 'failed') {
        expect(state.kind === 'failed' && view.word === FAILURE_WORDS[state.reason]).toBe(true);
        // A removed host cannot be asked again; any other failure can.
        expect(view.retry).toBe(state.kind === 'failed' && state.reason !== 'closed');
      }
      if (view.kind === 'waiting') expect(view.word).not.toBe('');
    }),
  );
});

test('every reason a subscription can fail for has its own word, and none is a sentence', () => {
  const words = [...RETRYABLE_REASONS, ...TERMINAL_REASONS].map(reason => FAILURE_WORDS[reason]);
  expect(
    words.every(word => word !== '' && !word.includes('.') && word.split(' ').length <= 2),
  ).toBe(true);
  // Reasons that mean different things to the person do not share a word, except the two
  // failures of the host's own program, which they cannot tell apart.
  expect(new Set(words).size).toBe(words.length - 1);
});

test('waiting says how far the host has come, and a retry says it is trying again', () => {
  const at = (step: (typeof CONNECT_STEPS)[number], attempt: number) =>
    statsView({kind: 'connecting', step, attempt});
  expect(at('gateway', 0)).toEqual({kind: 'waiting', word: 'Connecting'});
  expect(at('handshake', 0)).toEqual({kind: 'waiting', word: 'Connecting'});
  expect(at('stream', 0)).toEqual({kind: 'waiting', word: 'Reading'});
  expect(at('stream', 2)).toEqual({kind: 'waiting', word: 'Retrying'});
});

test('a sample is its gauges: CPU, memory, and the GPUs when the host has any', () => {
  const gauges = statsGauges(
    sample({
      cpuPercent: 34.4,
      memory: {usedBytes: 12 * GIB, totalBytes: 36 * GIB},
      gpus: [
        {name: 'H100', utilizationPercent: 100, usedBytes: 40 * GIB, totalBytes: 80 * GIB},
        {name: 'H100', utilizationPercent: 50, usedBytes: 20 * GIB, totalBytes: 80 * GIB},
      ],
    }),
  );
  expect(gauges.map(gauge => [gauge.label, gauge.figure])).toEqual([
    ['CPU', '34%'],
    ['Memory', '12.0 / 36.0 GB'],
    ['GPU', '75%'],
    ['VRAM', '60 / 160 GB'],
  ]);
  expect(gauges.map(gauge => gauge.share)).toEqual([0.344, 1 / 3, 0.75, 0.375]);
});

test('a host without GPUs shows none, and no reading is never drawn as zero', () => {
  expect(statsGauges(sample({})).map(gauge => gauge.label)).toEqual(['CPU', 'Memory']);
  const unreadable = statsGauges(sample({cpuPercent: null, memory: null, gpus: null}));
  // The GPU tool is missing: this host has no GPU to read. The CPU and memory ones are misses.
  expect(unreadable.map(gauge => [gauge.label, gauge.share, gauge.figure])).toEqual([
    ['CPU', null, '–'],
    ['Memory', null, '–'],
    ['GPU', null, '–'],
  ]);
  const withoutTool = sample({});
  expect(
    statsGauges({
      ...withoutTool,
      gpu: {case: 'gpuUnavailable', value: {reason: AbsenceReason.TOOL_NOT_FOUND}},
    } as never).map(gauge => gauge.label),
  ).toEqual(['CPU', 'Memory']);
});

test('a reading that is nearly full is drawn hot, and shares stay inside the meter', () => {
  fc.assert(
    fc.property(fc.double({min: 0, max: 100, noNaN: true}), cpuPercent => {
      const [cpu] = statsGauges(sample({cpuPercent}));
      expect(cpu?.share).toBeGreaterThanOrEqual(0);
      expect(cpu?.share).toBeLessThanOrEqual(1);
      expect(cpu?.tone).toBe(cpuPercent >= 90 ? 'accent' : 'ok');
    }),
  );
});

test('an age reads in the largest whole unit, never negative, and never goes down with time', () => {
  expect(ageWord(0, 0)).toBe('0 s');
  expect(ageWord(59_999, 0)).toBe('59 s');
  expect(ageWord(60_000, 0)).toBe('1 min');
  expect(ageWord(3_599_999, 0)).toBe('59 min');
  expect(ageWord(3_600_000, 0)).toBe('1 h');
  expect(ageWord(0, 5_000)).toBe('0 s');
  const order = (word: string): number => {
    const [amount = '0', unit = 's'] = word.split(' ');
    return Number(amount) * (unit === 's' ? 1 : unit === 'min' ? 60 : 3600);
  };
  fc.assert(
    fc.property(fc.nat({max: 1e8}), fc.nat({max: 1e8}), (early, gap) => {
      expect(order(ageWord(early + gap, 0))).toBeGreaterThanOrEqual(order(ageWord(early, 0)));
    }),
  );
});
