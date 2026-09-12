import type {ProtocolResponse, RunEvent} from '@vibesys/backend-client';
import {roundNumberFromLabel} from '@vibesys/core-state';

interface PerfPoint {
  round: number;
  metric: string;
  value: number;
  unit: string;
}

type PerformanceContext = NonNullable<ProtocolResponse['performance_context']>;

const PLOT_HEIGHT = 8;
/**
 * Plot columns, excluding the axis gutter. `right-pane.ts` derives the pane's
 * minimum width from this so the two cannot drift apart.
 */
export const PLOT_WIDTH = 48;
const CONTEXT_LABEL_WIDTH = 10;

export function renderPerformanceCurve(
  performance: ProtocolResponse['performance'] | undefined,
  events?: RunEvent[],
  context?: ProtocolResponse['performance_context'],
): string {
  const points = performancePoints(performance, events);
  const contextLines = contextSection(context ?? null);
  if (points.length === 0) {
    return [...contextLines, 'No performance data yet.'].join('\n');
  }

  // Points are still keyed by the recorded unit string; the backend context
  // only names the series, so it must not change which points are plotted.
  const series = latestMetric(points);
  const metric = context?.objective_metric ?? series;
  const visible = points.filter(point => point.metric === series);
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

  const lines = [`Performance · ${metric}`, ...contextLines];
  for (let row = 0; row < PLOT_HEIGHT; row += 1) {
    const value = maxValue - ((maxValue - minValue) * row) / Math.max(1, PLOT_HEIGHT - 1);
    lines.push(`${formatAxis(value).padStart(8)} ┤${(grid[row] ?? []).join('')}`);
  }
  lines.push(`         └${'─'.repeat(PLOT_WIDTH)}`);
  // The row must total the same 10 + PLOT_WIDTH columns as the axis and
  // border rows above, with the left label starting under the plot's first
  // column (right after the border row's '└'). Padding the right label to a
  // fixed PLOT_WIDTH - 2 assumed a single-digit minRound; pad relative to the
  // actual left-label length instead so double-digit rounds still align.
  const minLabel = `r${minRound}`;
  const maxLabel = `r${maxRound}`;
  lines.push(`${''.padStart(10)}${minLabel}${maxLabel.padStart(PLOT_WIDTH - minLabel.length)}`);

  const best = visible.reduce((current, point) => (point.value > current.value ? point : current));
  const latest = visible.at(-1);
  if (latest) {
    lines.push(
      `best r${best.round} ${formatValue(best.value)} ${unit} · latest r${latest.round} ${formatValue(latest.value)} ${unit}`,
    );
  }
  return lines.join('\n');
}

function performancePoints(
  performance: ProtocolResponse['performance'] | undefined,
  events: RunEvent[] | undefined,
): PerfPoint[] {
  const byRound = new Map<number, PerfPoint>();
  for (const round of performance ?? []) {
    byRound.set(round.round, {
      round: round.round,
      metric: round.perf_unit,
      value: round.perf_metric,
      unit: round.perf_unit,
    });
  }
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
    // The measurement `benchmark_result` used to carry rides a completed
    // benchmark gate on new journals (#692); the map keyed by round keeps a
    // journal carrying both kinds from double-counting.
    if (
      data?.kind === 'gate_finished' &&
      data.gate === 'benchmark' &&
      event.status !== 'failed' &&
      data.metric != null &&
      data.value != null
    ) {
      byRound.set(round, {
        round,
        metric: data.metric,
        value: data.value,
        unit: data.unit ?? data.metric,
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

/**
 * The objective facts above the plot: metric, unit, direction, baseline, and
 * how the benchmark measures. Direction is words plus a glyph, never color,
 * and absent facts drop their line rather than render a placeholder.
 */
function contextSection(context: PerformanceContext | null): string[] {
  if (context === null) return [];
  const lines: string[] = [];
  if (context.objective_metric) {
    lines.push(contextLine('Metric', metricSummary(context.objective_metric, context)));
  }
  const baseline = baselineSummary(context);
  if (baseline !== null) lines.push(contextLine('Baseline', baseline));
  if (context.objective_description) {
    lines.push(contextLine('Measures', context.objective_description));
  }
  if (lines.length === 0) return [];
  lines.push('');
  return lines;
}

function contextLine(label: string, value: string): string {
  return `${label.padEnd(CONTEXT_LABEL_WIDTH)}${value}`;
}

function metricSummary(metric: string, context: PerformanceContext): string {
  const parts = [metric];
  // Legacy rounds record the metric name in the unit slot; repeating it as a
  // parenthesized unit would read as noise.
  if (context.objective_unit && context.objective_unit !== metric) {
    parts.push(`(${context.objective_unit})`);
  }
  const summary = parts.join(' ');
  if (context.objective_direction === 'max') return `${summary} · maximize ↑`;
  if (context.objective_direction === 'min') return `${summary} · minimize ↓`;
  return summary;
}

function baselineSummary(context: PerformanceContext): string | null {
  const parts: string[] = [];
  if (context.objective_baseline_value != null) parts.push(trim(context.objective_baseline_value));
  if (context.objective_baseline_round != null) parts.push(`r${context.objective_baseline_round}`);
  if (context.objective_baseline_commit) {
    parts.push(`commit ${context.objective_baseline_commit.slice(0, 7)}`);
  }
  return parts.length > 0 ? parts.join(' · ') : null;
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
