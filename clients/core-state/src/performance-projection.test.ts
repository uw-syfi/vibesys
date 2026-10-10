import {describe, expect, test} from 'bun:test';
import type {HypothesisEntry, ProtocolResponse, RunEvent} from '@vibesys/backend-client';
import {
  formatPerformanceDelta,
  initialCoreState,
  projectPerformance,
  recordsBenchmark,
  reduceEventBatch,
} from './index.js';

describe('performance projection', () => {
  test('normalizes persisted, legacy, and gate measurements by round', () => {
    const projection = projectPerformance({
      performance: [performance(1, 5), performance(2, 10)],
      events: [benchmark(1, 2, 12), benchmarkGate(2, 3, 15), roundFinished(3, 4, 20)],
      experiments: [experiment('max', 4, 25)],
    });

    expect(projection).toEqual({
      direction: 'max',
      points: [
        {round: 1, metric: 'ops/s', value: 5, unit: 'ops/s', formattedDelta: null},
        {round: 2, metric: 'throughput', value: 12, unit: 'ops/s', formattedDelta: null},
        {round: 3, metric: 'throughput', value: 15, unit: 'ops/s', formattedDelta: null},
        {round: 4, metric: 'ops/s', value: 20, unit: 'ops/s', formattedDelta: '+25%'},
      ],
    });
  });

  test('uses recorded perf_direction and rejects a mixed direction as one series', () => {
    expect(projectPerformance({experiments: [experiment('min', 1, -2.25)]}).direction).toBe('min');
    expect(
      projectPerformance({
        experiments: [experiment('max', 1, 5), experiment('min', 2, -5)],
      }).direction,
    ).toBeNull();
  });

  test('prefers the official performance direction over stale experiment records', () => {
    expect(
      projectPerformance({
        objectiveDirection: 'min',
        experiments: [experiment('max', 1, 5)],
      }).direction,
    ).toBe('min');
  });

  test('formats every delta through the same compact contract', () => {
    expect([-12.4, -1.25, 0, 1.25, 12.4].map(formatPerformanceDelta)).toEqual([
      '-12%',
      '-1.3%',
      '0.0%',
      '+1.3%',
      '+12%',
    ]);
  });

  test('the predicate recognizes every event carrying an inline measurement', () => {
    const measured = [benchmark(1, 1, 1), benchmarkGate(2, 2, 2), roundFinished(3, 3, 3)];
    const reused = benchmarkGate(5, 5, 5);
    if (reused.data?.kind !== 'gate_finished') throw new Error('expected benchmark gate');
    const ignored: RunEvent[] = [
      {...benchmarkGate(4, 4, 4), status: 'failed'},
      {...reused, data: {...reused.data, reused: true}},
    ];

    for (const event of measured) {
      expect(recordsBenchmark(event)).toBe(true);
      expect(projectPerformance({events: [event]}).points).toHaveLength(1);
    }
    for (const event of ignored) {
      expect(recordsBenchmark(event)).toBe(false);
      expect(projectPerformance({events: [event]}).points).toHaveLength(0);
    }
  });

  test('keeps legacy untyped performance-log invalidations refresh-worthy', () => {
    for (const type of ['benchmark_result', 'round_finished'] as const) {
      const event: RunEvent = {
        sequence: 7,
        timestamp: timestamp(7),
        type,
        round_label: 'round-7',
      };
      expect(recordsBenchmark(event)).toBe(true);
      expect(projectPerformance({events: [event]}).points).toEqual([]);
    }
  });

  test('matches the durable CoreState fold for primary measurement spellings', () => {
    const events = [benchmark(1, 1, 42.5), benchmarkGate(2, 2, 50)];
    const state = reduceEventBatch(initialCoreState(), events);
    const projection = projectPerformance({events});

    expect(projection.points).toEqual(
      state.benchmarks.flatMap(record =>
        record.roundNumber === null
          ? []
          : [
              {
                round: record.roundNumber,
                metric: record.metric,
                value: record.value,
                unit: record.unit,
                formattedDelta: null,
              },
            ],
      ),
    );
    expect(projection.direction).toBeNull();
  });

  test('uses round_finished only as a legacy fallback beside a primary measurement', () => {
    const projection = projectPerformance({
      events: [roundFinished(1, 1, 25), benchmarkGate(2, 2, 50), roundFinished(3, 2, 60)],
    });

    expect(projection.points).toEqual([
      {round: 1, metric: 'ops/s', value: 25, unit: 'ops/s', formattedDelta: null},
      {round: 2, metric: 'throughput', value: 50, unit: 'ops/s', formattedDelta: null},
    ]);
  });
});

function performance(
  round: number,
  value: number,
): NonNullable<ProtocolResponse['performance']>[number] {
  return {round, perf_metric: value, perf_unit: 'ops/s', passed: true};
}

function benchmark(sequence: number, round: number, value: number): RunEvent {
  return {
    sequence,
    timestamp: timestamp(sequence),
    type: 'benchmark_result',
    round_label: `round-${round}`,
    data: {kind: 'benchmark_result', metric: 'throughput', value, unit: 'ops/s'},
  };
}

function benchmarkGate(sequence: number, round: number, value: number): RunEvent {
  return {
    sequence,
    timestamp: timestamp(sequence),
    type: 'gate_finished',
    status: 'completed',
    round_label: `round-${round}`,
    data: {
      kind: 'gate_finished',
      gate: 'benchmark',
      metric: 'throughput',
      value,
      unit: 'ops/s',
    },
  };
}

function roundFinished(sequence: number, round: number, value: number): RunEvent {
  return {
    sequence,
    timestamp: timestamp(sequence),
    type: 'round_finished',
    status: 'completed',
    round_label: `round-${round}`,
    data: {
      kind: 'round_finished',
      attempts: 1,
      judge_verdict: 'pass',
      perf_metric: value,
      perf_unit: 'ops/s',
      profile_skipped: false,
    },
  };
}

function experiment(
  direction: NonNullable<HypothesisEntry['perf_direction']>,
  round: number,
  delta: number,
): HypothesisEntry {
  return {
    hypothesis_id: `H-${round}`,
    first_round: round,
    last_round: round,
    perf_direction: direction,
    perf_delta_pct: delta,
    rounds: [{round, passed: true, reviewed: true, perf_delta_pct: delta}],
  };
}

function timestamp(sequence: number): string {
  return new Date(1_767_225_600_000 + sequence).toISOString();
}
