import {describe, expect, it} from 'bun:test';
import type {ProtocolResponse, RunEvent} from '@vibesys/backend-client';
import {PLOT_WIDTH, renderPerformanceCurve} from './performance-chart.js';

describe('renderPerformanceCurve', () => {
  it('plots persisted performance records by round', () => {
    const chart = renderPerformanceCurve([
      performance(1, 1000),
      performance(2, 2000),
      performance(3, 1500),
    ]);

    expect(chart).toContain('Performance · total_ops_per_sec');
    expect(chart).toContain('r1');
    expect(chart).toContain('r3');
    expect(chart).toContain('best r2 2k total_ops_per_sec');
    expect(chart).toContain('latest r3 1.5k total_ops_per_sec');
    expect(chart.match(/●/g)).toHaveLength(3);
  });

  it('falls back to benchmark events', () => {
    const chart = renderPerformanceCurve(
      [],
      [benchmark(1, 1, 1000), benchmark(2, 2, 2000), benchmark(3, 3, 1500)],
    );

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

  it('plots completed benchmark gate events (#692)', () => {
    const chart = renderPerformanceCurve(
      [],
      [benchmarkGate(1, 1, 1000), benchmarkGate(2, 2, 2000), benchmarkGate(3, 3, 1500)],
    );

    expect(chart).toContain('Performance · total_ops_per_sec');
    expect(chart).toContain('best r2 2k ops/s');
    expect(chart).toContain('latest r3 1.5k ops/s');
    expect(chart.match(/●/g)).toHaveLength(3);
  });

  it('counts a round once when both benchmark spellings land on it', () => {
    const chart = renderPerformanceCurve([], [benchmark(1, 1, 1000), benchmarkGate(2, 1, 1000)]);

    expect(chart).toContain('best r1 1k ops/s');
    expect(chart.match(/●/g)).toHaveLength(1);
  });

  it('ignores failed and measurement-free benchmark gates', () => {
    const failed: RunEvent = {...benchmarkGate(1, 1, 1000), status: 'failed'};
    const bare = benchmarkGate(2, 2, 1000);
    if (bare.data?.kind === 'gate_finished') bare.data = {...bare.data, metric: null, value: null};

    expect(renderPerformanceCurve([], [failed, bare])).toBe('No performance data yet.');
  });

  it('titles the plot with the backend metric name instead of the unit', () => {
    const record = {...performance(1, 1000), perf_unit: 'ops/s'};
    const chart = renderPerformanceCurve([record], [], context({objective_unit: 'ops/s'}));

    expect(chart).toContain('Performance · total_ops_per_sec');
    expect(chart).not.toContain('Performance · ops/s');
    expect(chart.match(/●/g)).toHaveLength(1);
  });

  it('states metric, unit, direction, and baseline above the plot', () => {
    const chart = renderPerformanceCurve(
      [performance(1, 1000), performance(2, 2000)],
      [],
      context({
        objective_unit: 'ops/s',
        objective_direction: 'max',
        objective_baseline_value: 1234.5,
        objective_baseline_round: 1,
        objective_baseline_commit: 'e17fce8123abc',
        objective_description: 'Throughput of the MPMC queue benchmark.',
      }),
    );

    expect(chart).toContain('Metric    total_ops_per_sec (ops/s) · maximize ↑');
    expect(chart).toContain('Baseline  1234.5 · r1 · commit e17fce8');
    expect(chart).toContain('Measures  Throughput of the MPMC queue benchmark.');
  });

  it('spells minimize with its glyph for a lower-is-better objective', () => {
    const chart = renderPerformanceCurve(
      [performance(1, 1000)],
      [],
      context({objective_metric: 'p99_latency_us', objective_direction: 'min'}),
    );

    expect(chart).toContain('Metric    p99_latency_us · minimize ↓');
  });

  it('drops the lines for facts the run never recorded', () => {
    const chart = renderPerformanceCurve([performance(1, 1000)], [], context({}));

    expect(chart).toContain('Metric    total_ops_per_sec');
    expect(chart).not.toContain('maximize');
    expect(chart).not.toContain('minimize');
    expect(chart).not.toContain('Baseline');
    expect(chart).not.toContain('Measures');
  });

  it('does not repeat a unit slot that just holds the metric name', () => {
    const chart = renderPerformanceCurve(
      [performance(1, 1000)],
      [],
      context({objective_unit: 'total_ops_per_sec'}),
    );

    expect(chart).not.toContain('(total_ops_per_sec)');
  });

  it('renders a description-only context when only the prose is known', () => {
    const chart = renderPerformanceCurve([], [], {
      objective_description: 'Maximize queue throughput.',
    });

    expect(chart).toContain('Measures  Maximize queue throughput.');
    expect(chart).not.toContain('Metric ');
    expect(chart.endsWith('No performance data yet.')).toBe(true);
  });

  it('shows the objective before the first measurement', () => {
    const chart = renderPerformanceCurve(
      [],
      [],
      context({objective_direction: 'max', objective_description: 'Ops per second.'}),
    );

    expect(chart).toContain('Metric    total_ops_per_sec · maximize ↑');
    expect(chart).toContain('Measures  Ops per second.');
    expect(chart.endsWith('No performance data yet.')).toBe(true);
    expect(chart).not.toContain('●');
  });
});

describe('chart geometry', () => {
  // The axis rows, bottom border row, and bottom round-label row are drawn
  // to look like one fixed-width grid. Unlike the free-text summary and
  // context lines (which the right pane word-wraps on purpose), these rows
  // must never exceed PLOT_WIDTH + 10 (8-char axis gutter + ' ┤' = 10, plus
  // the PLOT_WIDTH plot columns) or they wrap and break the grid.
  function structuralRows(chart: string): {
    axisRows: string[];
    borderRow: string;
    labelRow: string;
  } {
    const lines = chart.split('\n');
    const axisRows = lines.filter(line => line.includes('┤'));
    const borderIndex = lines.findIndex(line => line.includes('└'));
    const borderRow = lines[borderIndex] ?? '';
    const labelRow = lines[borderIndex + 1] ?? '';
    return {axisRows, borderRow, labelRow};
  }

  it('keeps every structural row within PLOT_WIDTH + 10 columns for single-digit rounds', () => {
    const chart = renderPerformanceCurve([
      performance(1, 1000),
      performance(2, 2000),
      performance(9, 1500),
    ]);
    const {axisRows, borderRow, labelRow} = structuralRows(chart);
    expect(axisRows.length).toBeGreaterThan(0);
    for (const row of [...axisRows, borderRow, labelRow]) {
      expect(row.length).toBeLessThanOrEqual(PLOT_WIDTH + 10);
    }
  });

  it('keeps every structural row within PLOT_WIDTH + 10 columns for double-digit rounds', () => {
    // Before the fix, the label row hardcoded its right-hand pad width
    // assuming a 2-character left label ('r' + a single digit), so a
    // double-digit minRound pushed the row past PLOT_WIDTH + 10.
    const chart = renderPerformanceCurve([
      performance(10, 1000),
      performance(11, 2000),
      performance(99, 1500),
    ]);
    const {axisRows, borderRow, labelRow} = structuralRows(chart);
    expect(axisRows.length).toBeGreaterThan(0);
    for (const row of [...axisRows, borderRow, labelRow]) {
      expect(row.length).toBeLessThanOrEqual(PLOT_WIDTH + 10);
    }
  });

  it('aligns the left round label under the plot area, in the same column the border row draws └ before', () => {
    const chart = renderPerformanceCurve([
      performance(10, 1000),
      performance(11, 2000),
      performance(99, 1500),
    ]);
    const {borderRow, labelRow} = structuralRows(chart);
    const plotStartColumn = borderRow.indexOf('└') + 1;
    expect(labelRow.indexOf('r10')).toBe(plotStartColumn);
  });
});

function context(
  overrides: Partial<NonNullable<ProtocolResponse['performance_context']>>,
): NonNullable<ProtocolResponse['performance_context']> {
  return {objective_metric: 'total_ops_per_sec', ...overrides};
}

function performance(
  round: number,
  value: number,
): NonNullable<ProtocolResponse['performance']>[number] {
  return {
    round,
    perf_metric: value,
    perf_unit: 'total_ops_per_sec',
    passed: true,
    profile_skipped: false,
  };
}

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

/** The measurement as a completed `gate_finished` benchmark carries it (#692). */
function benchmarkGate(sequence: number, round: number, value: number): RunEvent {
  return {
    sequence,
    timestamp: '2026-01-01T00:00:00Z',
    type: 'gate_finished',
    status: 'completed',
    agent_kind: null,
    round_label: `round-${round}`,
    data: {
      kind: 'gate_finished',
      gate: 'benchmark',
      metric: 'total_ops_per_sec',
      value,
      unit: 'ops/s',
    },
  };
}
