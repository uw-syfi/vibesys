import {expect, test} from 'bun:test';
import {
  CONNECT_STEPS,
  type FailureReason,
  type MachineStats,
  RETRYABLE_REASONS,
  type StaleReason,
  type SubscriptionState,
  TERMINAL_REASONS,
} from '@vibesys/subscription';
import {machineEntry, type Readings} from '@vibesys/subscription/testing';
import fc from 'fast-check';
import {
  ageWord,
  cardStatus,
  FAILURE_TONES,
  FAILURE_WORDS,
  type GaugeView,
  initialMachines,
  type Machine,
  machinesStep,
  statsGauges,
  statsView,
  streamStatus,
} from './index.js';

const GIB = 2 ** 30;

function sample(readings: Readings): MachineStats {
  const {value} = machineEntry('machine', 1_000, readings);
  if (value.case !== 'machineStats') throw new Error('not stats');
  return value.value;
}

/** Readings as the contract allows them: CPU 0 to 100, used within a total above zero, 0 to 256 GPUs. */
const SIZE = fc.integer({min: 1, max: 2 ** 40});
const MEMORY = SIZE.chain(total =>
  fc.integer({min: 0, max: total}).map(used => ({usedBytes: used, totalBytes: total})),
);
const GPU = SIZE.chain(total =>
  fc.record({
    name: fc.constant('GPU'),
    utilizationPercent: fc.double({min: 0, max: 100, noNaN: true}),
    usedBytes: fc.integer({min: 0, max: total}),
    totalBytes: fc.constant(total),
  }),
);
const READINGS: fc.Arbitrary<Readings> = fc.record({
  cpuPercent: fc.oneof(fc.constant(null), fc.double({min: 0, max: 100, noNaN: true})),
  memory: fc.oneof(fc.constant(null), MEMORY),
  gpus: fc.oneof(
    fc.constant(null),
    fc.constant('no-tool' as const),
    fc.array(GPU, {maxLength: 256}),
  ),
});

const STALE_REASON = fc.constantFrom<StaleReason>(...RETRYABLE_REASONS);
const FAILURE = fc.constantFrom<FailureReason>(...RETRYABLE_REASONS, ...TERMINAL_REASONS);
const STATE: fc.Arbitrary<SubscriptionState<MachineStats>> = fc.oneof(
  fc.record({
    kind: fc.constant('connecting' as const),
    step: fc.constantFrom(...CONNECT_STEPS),
    attempt: fc.nat({max: 9}),
  }),
  fc.record({
    kind: fc.constant('live' as const),
    value: READINGS.map(sample),
    since: fc.nat(),
  }),
  fc.record({
    kind: fc.constant('stale' as const),
    value: READINGS.map(sample),
    reason: STALE_REASON,
    attempt: fc.integer({min: 0, max: 4}),
    valueAt: fc.nat(),
  }),
  fc.record({kind: fc.constant('failed' as const), reason: FAILURE, retrying: fc.boolean()}),
);

function listed(status: 'ready' | 'needs-update' | 'unavailable' | 'sign-in'): Machine {
  let {state} = initialMachines();
  state = machinesStep(state, {
    type: 'listed',
    token: 1,
    result: {ok: true, value: [{host: 'ssh:host-a', label: 'host-a', checkout: '~/vibesys'}]},
  }).state;
  const [machine] = state.machines;
  if (machine === undefined) throw new Error('no machine');
  const facts = {
    checkout: null,
    hardware: {cpu_cores: 8, gpus: [], accelerator: 'cpu'},
    docker: {reachable: true, version: '29.1'},
    harness_clis: [],
    runs: {live: 0, ended_today: 0},
  };
  const report =
    status === 'ready'
      ? {kind: 'ready' as const, facts}
      : status === 'needs-update'
        ? {kind: 'needs-update' as const, facts}
        : {kind: status, detail: 'x'};
  return {...machine, report, checking: null} as Machine;
}

test('property: a value is shown exactly while the subscription holds one, and stale only when it is', () => {
  fc.assert(
    fc.property(STATE, state => {
      const view = statsView(state);
      expect(view.kind === 'values').toBe(state.kind === 'live' || state.kind === 'stale');
      if (view.kind === 'values') {
        expect(view.stale === null).toBe(state.kind === 'live');
        if (state.kind === 'stale') {
          expect(view.stale?.valueAt).toBe(state.valueAt);
          // A stream being opened right now is told from one waited for.
          expect(view.stale?.reconnecting).toBe(state.attempt === 0);
        }
      }
      if (view.kind === 'failed') {
        expect(state.kind === 'failed' && view.reason === state.reason).toBe(true);
        // A removed host cannot be asked again; any other failure can.
        expect(view.retry).toBe(state.kind === 'failed' && state.reason !== 'closed');
      }
      if (view.kind === 'waiting') expect(state.kind).toBe('connecting');
    }),
  );
});

/** The labels a sample's gauges carry: CPU and memory, and the GPU pair when the host reports GPUs. */
function labelsOf(readings: Readings): GaugeView['label'][] {
  const reported =
    readings.gpus === null || (Array.isArray(readings.gpus) && readings.gpus.length > 0);
  return reported ? ['CPU', 'Memory', 'GPU', 'VRAM'] : ['CPU', 'Memory'];
}

function checkGauge(gauge: GaugeView): void {
  expect(gauge.figure).not.toBe('');
  expect(gauge.figure).not.toMatch(/NaN|Infinity/);
  // The figure fits the room the gauge keeps for it, so a changing figure never moves a neighbor.
  expect(gauge.width).toBeGreaterThanOrEqual(gauge.figure.length);
  if (gauge.share === null) {
    expect(gauge.figure).toBe('–');
    return;
  }
  expect(gauge.share).toBeGreaterThanOrEqual(0);
  expect(gauge.share).toBeLessThanOrEqual(1);
  // Hot is the warn tone, from nine tenths, and nothing else is.
  expect(gauge.tone).toBe(gauge.share >= 0.9 ? 'warn' : 'ok');
}

test('property: whatever a host reads, its gauges are bounded, complete, and never zero for a reading not made', () => {
  fc.assert(
    fc.property(READINGS, readings => {
      const gauges = statsGauges(sample(readings));
      expect(gauges.map(gauge => gauge.label)).toEqual(labelsOf(readings));
      for (const gauge of gauges) checkGauge(gauge);
      // A reading that was not made is empty, never a zero.
      expect(gauges[0]?.share === null).toBe(readings.cpuPercent === null);
      expect(gauges[1]?.share === null).toBe(readings.memory === null);
    }),
  );
});

test('property: a figure that changes keeps the room of its gauge, so nothing beside it moves', () => {
  fc.assert(
    fc.property(SIZE, fc.nat({max: 100}), fc.nat({max: 100}), (total, a, b) => {
      const at = (percent: number) =>
        statsGauges(
          sample({
            cpuPercent: percent,
            memory: {usedBytes: Math.floor((total * percent) / 100), totalBytes: total},
          }),
        );
      const [first, second] = [at(a), at(b)];
      expect(first.map(gauge => gauge.width)).toEqual(second.map(gauge => gauge.width));
      // Read and unread share the CPU's room, the one that changes most.
      expect(statsGauges(sample({cpuPercent: null}))[0]?.width).toBe(first[0]?.width);
    }),
  );
});

test('every reason a subscription can fail for has its own word and a tone, and none is a sentence', () => {
  const reasons = [...RETRYABLE_REASONS, ...TERMINAL_REASONS];
  const words = reasons.map(reason => FAILURE_WORDS[reason]);
  expect(
    words.every(word => word !== '' && !word.includes('.') && word.split(' ').length <= 2),
  ).toBe(true);
  // Reasons that mean different things to the person do not share a word, except the two failures
  // of the host's own program, which they cannot tell apart.
  expect(new Set(words).size).toBe(words.length - 1);
  for (const reason of reasons) expect(['neutral', 'warn', 'err']).toContain(FAILURE_TONES[reason]);
  // A host that never could is not the same as one that broke, and a removed one is no alarm.
  expect(FAILURE_TONES.unsupported).toBe('warn');
  expect(FAILURE_TONES.closed).toBe('neutral');
});

test('a card has one status: the stream says how its readings are, and a connection that is the problem says so alone', () => {
  const live = streamStatus(statsView({kind: 'live', value: sample({}), since: 0}));
  expect(live).toEqual({word: 'Online', tone: 'ok'});
  expect(cardStatus(listed('ready'), null)).toEqual({word: 'Online', tone: 'ok'});
  expect(
    cardStatus(listed('ready'), statsView({kind: 'connecting', step: 'stream', attempt: 0})),
  ).toEqual({word: 'Connecting', tone: 'neutral'});
  const stale = (attempt: number) =>
    statsView({kind: 'stale', value: sample({}), reason: 'silent', attempt, valueAt: 0});
  expect(cardStatus(listed('ready'), stale(2))).toEqual({word: 'Stale', tone: 'warn'});
  // Right after a Retry the stream is being opened: the last values stay, and the word says so.
  expect(cardStatus(listed('ready'), stale(0))).toEqual({word: 'Connecting', tone: 'neutral'});
  const failed = statsView({kind: 'failed', reason: 'unreachable', retrying: true});
  expect(cardStatus(listed('ready'), failed)).toEqual({word: 'Unreachable', tone: 'err'});
  // The connection's own problem names its fix, and the stream adds nothing to it.
  expect(cardStatus(listed('sign-in'), failed).word).toBe('Sign in');
  expect(cardStatus(listed('unavailable'), failed).word).toBe('Unreachable');
  // An older host is `Not supported` once and not also `Needs update`.
  const unsupported = statsView({kind: 'failed', reason: 'unsupported', retrying: false});
  expect(cardStatus(listed('needs-update'), unsupported)).toEqual({
    word: 'Not supported',
    tone: 'warn',
  });
  expect(cardStatus(listed('needs-update'), null).word).not.toBe('Online');
});

test('property: a card never says Online beside a stream that is not live', () => {
  fc.assert(
    fc.property(
      STATE,
      fc.constantFrom('ready', 'needs-update', 'unavailable', 'sign-in' as const),
      (state, probe) => {
        const status = cardStatus(listed(probe), statsView(state));
        if (status.word === 'Online') {
          expect(probe === 'ready' && state.kind === 'live').toBe(true);
        }
        // One word, never a sentence, and a tone the chip can draw.
        expect(status.word.split(' ').length).toBeLessThanOrEqual(3);
        expect(['neutral', 'ok', 'warn', 'err']).toContain(status.tone);
      },
    ),
  );
});

test('a reading that is nearly full is drawn in the warn tone, and shares stay inside the meter', () => {
  fc.assert(
    fc.property(fc.double({min: 0, max: 100, noNaN: true}), cpuPercent => {
      const [cpu] = statsGauges(sample({cpuPercent}));
      expect(cpu?.share).toBeGreaterThanOrEqual(0);
      expect(cpu?.share).toBeLessThanOrEqual(1);
      expect(cpu?.tone).toBe(cpuPercent >= 90 ? 'warn' : 'ok');
    }),
  );
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

test('a host without GPUs shows none, and every section that was not read is `–` and empty', () => {
  expect(statsGauges(sample({})).map(gauge => gauge.label)).toEqual(['CPU', 'Memory']);
  expect(statsGauges(sample({gpus: 'no-tool'})).map(gauge => gauge.label)).toEqual([
    'CPU',
    'Memory',
  ]);
  const unreadable = statsGauges(sample({cpuPercent: null, memory: null, gpus: null}));
  expect(unreadable.map(gauge => [gauge.label, gauge.share, gauge.figure])).toEqual([
    ['CPU', null, '–'],
    ['Memory', null, '–'],
    ['GPU', null, '–'],
    ['VRAM', null, '–'],
  ]);
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
