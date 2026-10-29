import {describe, expect, it} from 'bun:test';
import {
  type Candidate,
  type MeasuredReading,
  type ObjectiveAxis,
  type TradeoffPoint,
  tradeoffOf,
} from './index.js';

/** A small seeded generator, so a failing case repeats. */
function random(seed: number): () => number {
  let state = seed;
  return () => {
    state = (state * 1664525 + 1013904223) % 4294967296;
    return state / 4294967296;
  };
}

const AXES: readonly [ObjectiveAxis, ObjectiveAxis] = [
  {name: 'throughput', direction: 'max'},
  {name: 'latency', direction: 'min'},
];

const row = (name: string, value: number, unit: string | null = null): MeasuredReading => ({
  name,
  value,
  unit,
  direction: name === 'latency' ? 'min' : 'max',
});

function candidate(
  id: string,
  outcome: Candidate['outcome'],
  at: number,
  readings: readonly MeasuredReading[],
  inputReadings: readonly MeasuredReading[] = [],
): Candidate {
  return {
    id,
    hypothesis: null,
    parent: null,
    title: `idea ${id}`,
    idea: null,
    round: null,
    outcome,
    reason: null,
    baseRound: null,
    measurement: {
      span: `gate-${id}`,
      at,
      metricValue: readings[0]?.value ?? null,
      vsBaseline: null,
      against: null,
      readings,
      inputReadings,
      verdict: null,
      failed: null,
    },
    attempts: [],
    startedAt: at - 1,
    endedAt: at,
    running: [],
  };
}

const INPUT = [row('throughput', 100, 'ops/s'), row('latency', 50, 'ms')];

describe('tradeoffOf', () => {
  it('has the two axes with the units the readings carry, the input, and a point per settled idea', () => {
    const found = tradeoffOf(
      [
        candidate(
          'a',
          'passed',
          10,
          [row('throughput', 120, 'ops/s'), row('latency', 40, 'ms')],
          INPUT,
        ),
        candidate('b', 'failed', 20, [row('throughput', 90), row('latency', 70)], INPUT),
        candidate('c', 'running', 30, [row('throughput', 200), row('latency', 10)], INPUT),
      ],
      AXES,
    );

    expect(found?.axes).toEqual([
      {name: 'throughput', direction: 'max', unit: 'ops/s'},
      {name: 'latency', direction: 'min', unit: 'ms'},
    ]);
    expect(found?.input).toEqual({x: 100, y: 50});
    expect(found?.points.map(p => [p.id, p.x, p.y, p.outcome])).toEqual([
      ['a', 120, 40, 'passed'],
      ['b', 90, 70, 'failed'],
    ]);
  });

  it('is null unless the run has exactly two objectives, and until a point exists', () => {
    const settled = [candidate('a', 'passed', 10, INPUT)];
    expect(tradeoffOf(settled, AXES.slice(0, 1))).toBeNull();
    expect(tradeoffOf(settled, [...AXES, {name: 'memory', direction: 'min'}])).toBeNull();
    expect(tradeoffOf(settled, [])).toBeNull();
    expect(tradeoffOf([], AXES)).toBeNull();
    expect(tradeoffOf([candidate('r', 'running', 5, INPUT)], AXES)).toBeNull();
  });

  it('leaves out an idea that measured only one axis, and keeps the input unknown without it', () => {
    const found = tradeoffOf(
      [
        candidate('a', 'passed', 10, [row('throughput', 120)], []),
        candidate('b', 'passed', 20, INPUT),
      ],
      AXES,
    );
    expect(found?.points.map(p => p.id)).toEqual(['b']);
    expect(found?.input).toBeNull();
  });

  it('keeps every settled idea with both readings, in the order measured, whatever the order given', () => {
    const next = random(3);
    for (let round = 0; round < 200; round += 1) {
      const count = 1 + Math.floor(next() * 12);
      const ideas = Array.from({length: count}, (_, index) => {
        const both = next() < 0.8;
        const readings = both
          ? [row('throughput', next() * 100), row('latency', next() * 100)]
          : [row('throughput', next() * 100)];
        const outcome =
          (['passed', 'failed', 'running'] as const)[Math.floor(next() * 3)] ?? 'passed';
        return candidate(`i${index}`, outcome, Math.floor(next() * 50), readings);
      });
      const shuffled = [...ideas].sort(() => next() - 0.5);
      const expected = ideas
        .filter(
          item => item.outcome !== 'running' && (item.measurement?.readings.length ?? 0) === 2,
        )
        .map(item => item.id)
        .sort();

      const found = tradeoffOf(shuffled, AXES);

      expect(found?.points.map(p => p.id).sort() ?? []).toEqual(expected);
      const times = (found?.points ?? []).map((p: TradeoffPoint) => p.at);
      expect(times).toEqual([...times].sort((a, b) => a - b));
    }
  });
});
