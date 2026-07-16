import {describe, expect, it} from 'vitest';
import {renderPerformanceCurve} from './performance-chart.js';
import type {RunEvent} from './protocol.js';

describe('renderPerformanceCurve', () => {
  it('plots benchmark results by round', () => {
    const chart = renderPerformanceCurve([
      benchmark(1, 1, 1000),
      benchmark(2, 2, 2000),
      benchmark(3, 3, 1500),
    ]);

    expect(chart).toContain('Performance · total_ops_per_sec');
    expect(chart).toContain('r1');
    expect(chart).toContain('r3');
    expect(chart).toContain('best r2 2k ops/s');
    expect(chart).toContain('latest r3 1.5k ops/s');
    expect(chart.match(/●/g)).toHaveLength(3);
  });

  it('handles missing data', () => {
    expect(renderPerformanceCurve([])).toBe('No performance data yet.');
  });
});

function benchmark(sequence: number, round: number, value: number): RunEvent {
  return {
    sequence,
    timestamp: '2026-01-01T00:00:00Z',
    type: 'benchmark_result',
    round_label: `round-${round}`,
    data: {
      kind: 'benchmark_result',
      metric: 'total_ops_per_sec',
      value,
      unit: 'ops/s',
    },
  };
}
