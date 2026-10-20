/**
 * The comparison baselines (`RunModel.baselines`) as the run page sets them beside its own
 * results: the measured ones as references (a value to read each idea against), the others as a
 * status word. A baseline applies to a metric when it names no metric or names that one, so a run's
 * readings are never set against another measure's.
 */
import type {ComparisonBaseline} from '@vibesys/run-state';
import type {Direction} from './direction.js';
import type {RunModel} from './run-model.js';
import {ratio} from './span-reading.js';

/** What a screen says of a comparison that has no reading. */
const STATUS_WORDS = {
  measured: 'not measured',
  failed: 'failed',
  not_configured: 'not configured',
  not_measured: 'not measured',
} as const satisfies Record<ComparisonBaseline['status'], string>;

/** A measured comparison: the value to read ideas against. */
export interface BaselineReference {
  readonly label: string;
  readonly value: number;
  readonly unit: string | null;
}

/** A comparison with no reading, by its status word. */
export interface BaselineNote {
  readonly label: string;
  readonly words: (typeof STATUS_WORDS)[ComparisonBaseline['status']];
}

/** The ratio as an improvement over the base, where `direction` says which way is better. */
export function improvement(ratioToBase: number, direction: Direction): number {
  return direction === 'min' && ratioToBase !== 0 ? 1 / ratioToBase : ratioToBase;
}

/** `value` over `base` as an improvement worded (`1.32×` is better); null when the base has no ratio. */
export function overBase(value: number, base: number, direction: Direction): string | null {
  return base > 0 ? ratio(improvement(value / base, direction)) : null;
}

/** The run's objective metric; null before the run declared one. */
export function objectiveMetric(model: Pick<RunModel, 'overview'>): string | null {
  const metric = model.overview?.objective.metric;
  return typeof metric === 'string' && metric !== '' ? metric : null;
}

function appliesTo(baseline: ComparisonBaseline, metric: string | null): boolean {
  return baseline.metric === null || metric === null || baseline.metric === metric;
}

/** The measured comparisons that apply to `metric` (null: any), in the task's order. */
export function baselineReferences(
  baselines: readonly ComparisonBaseline[],
  metric: string | null,
): readonly BaselineReference[] {
  return baselines.flatMap(baseline =>
    baseline.status === 'measured' && baseline.value !== null && appliesTo(baseline, metric)
      ? [{label: baseline.label, value: baseline.value, unit: baseline.unit}]
      : [],
  );
}

/** The comparisons without a reading, in the task's order, each with its status word. */
export function baselineNotes(baselines: readonly ComparisonBaseline[]): readonly BaselineNote[] {
  return baselines
    .filter(baseline => baseline.status !== 'measured' || baseline.value === null)
    .map(baseline => ({label: baseline.label, words: STATUS_WORDS[baseline.status]}));
}

/** The ideas table's ratio columns: one per comparison, a cell per idea. */
export interface BaselineColumns {
  readonly labels: readonly string[];
  /** The cells of each idea's id, in the order of `labels`; `–` when the idea or the comparison has no reading. */
  readonly cells: ReadonlyMap<string, readonly string[]>;
}

const NO_RATIO = '–';

export function baselineColumns(
  model: Pick<RunModel, 'baselines' | 'candidates' | 'direction' | 'overview'>,
): BaselineColumns {
  const metric = objectiveMetric(model);
  const columns = model.baselines.filter(baseline => appliesTo(baseline, metric));
  const cells = new Map<string, readonly string[]>();
  for (const idea of model.candidates.candidates) {
    const value = idea.measurement?.metricValue ?? null;
    cells.set(
      idea.id,
      columns.map(baseline =>
        value === null || baseline.status !== 'measured' || baseline.value === null
          ? NO_RATIO
          : (overBase(value, baseline.value, model.direction) ?? NO_RATIO),
      ),
    );
  }
  return {labels: columns.map(baseline => baseline.label), cells};
}
