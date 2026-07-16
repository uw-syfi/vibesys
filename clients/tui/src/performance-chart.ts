import type {RunEvent} from './protocol.js';
import {roundNumberFromLabel} from './run-map.js';

interface PerfPoint {
  round: number;
  metric: string;
  value: number;
  unit: string;
}

const PLOT_HEIGHT = 8;
const PLOT_WIDTH = 48;

export function renderPerformanceCurve(events: RunEvent[] | undefined): string {
  const points = performancePoints(events);
  if (points.length === 0) return 'No performance data yet.';

  const metric = latestMetric(points);
  const visible = points.filter(point => point.metric === metric);
  const values = visible.map(point => point.value);
  const minValue = Math.min(...values);
  const maxValue = Math.max(...values);
  const minRound = Math.min(...visible.map(point => point.round));
  const maxRound = Math.max(...visible.map(point => point.round));
  const unit = visible.at(-1)?.unit ?? metric;
  const grid: string[][] = Array.from({length: PLOT_HEIGHT}, () => Array(PLOT_WIDTH).fill(' '));

  for (const point of visible) {
    const x = scale(point.round, minRound, maxRound, 0, PLOT_WIDTH - 1);
    const y = PLOT_HEIGHT - 1 - scale(point.value, minValue, maxValue, 0, PLOT_HEIGHT - 1);
    const row = grid[y];
    if (row) row[x] = '●';
  }

  const lines = [`Performance · ${metric}`];
  for (let row = 0; row < PLOT_HEIGHT; row += 1) {
    const value = maxValue - ((maxValue - minValue) * row) / Math.max(1, PLOT_HEIGHT - 1);
    lines.push(`${formatAxis(value).padStart(8)} ┤${(grid[row] ?? []).join('')}`);
  }
  lines.push(`         └${'─'.repeat(PLOT_WIDTH)}`);
  lines.push(`${''.padStart(11)}r${minRound}${String(`r${maxRound}`).padStart(PLOT_WIDTH - 2)}`);

  const best = visible.reduce((current, point) => (point.value > current.value ? point : current));
  const latest = visible.at(-1);
  if (latest) {
    lines.push(
      `best r${best.round} ${formatValue(best.value)} ${unit} · latest r${latest.round} ${formatValue(latest.value)} ${unit}`,
    );
  }
  return lines.join('\n');
}

function performancePoints(events: RunEvent[] | undefined): PerfPoint[] {
  const byRound = new Map<number, PerfPoint>();
  for (const event of events ?? []) {
    const round = roundNumberFromLabel(event.round_label);
    if (round === null) continue;
    const data = event.data;
    if (data?.kind === 'benchmark_result') {
      byRound.set(round, {
        round,
        metric: data.metric,
        value: data.value,
        unit: data.unit,
      });
    }
    if (data?.kind === 'round_finished' && typeof data.perf_metric === 'number') {
      byRound.set(round, {
        round,
        metric: data.perf_unit ?? 'performance',
        value: data.perf_metric,
        unit: data.perf_unit ?? 'performance',
      });
    }
  }
  return [...byRound.values()].sort((a, b) => a.round - b.round);
}

function latestMetric(points: PerfPoint[]): string {
  return points.at(-1)?.metric ?? 'performance';
}

function scale(
  value: number,
  minInput: number,
  maxInput: number,
  minOutput: number,
  maxOutput: number,
): number {
  if (maxInput === minInput) return Math.round((minOutput + maxOutput) / 2);
  const ratio = (value - minInput) / (maxInput - minInput);
  return Math.round(minOutput + ratio * (maxOutput - minOutput));
}

function formatAxis(value: number): string {
  return formatValue(value);
}

function formatValue(value: number): string {
  const abs = Math.abs(value);
  if (abs >= 1_000_000_000) return `${trim(value / 1_000_000_000)}B`;
  if (abs >= 1_000_000) return `${trim(value / 1_000_000)}M`;
  if (abs >= 1_000) return `${trim(value / 1_000)}k`;
  return trim(value);
}

function trim(value: number): string {
  if (Number.isInteger(value)) return String(value);
  return value.toFixed(2).replace(/\.?0+$/, '');
}
