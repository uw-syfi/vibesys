/**
 * The trade-off chart of a run with two objectives: its points and axes in the words the screen
 * uses, over the whole run and its time bar. The data is `tradeoffOf` (a fold in
 * `@vibesys/run-state`); this only names things and puts times on the run's clock in minutes.
 */
import {type ObjectiveAxis, tradeoffOf} from '@vibesys/run-state/candidates';
import {metricLabel} from './metric-words.js';
import type {RunModel} from './run-model.js';

/** An axis of the chart: its name in words, its unit (empty when the run reports none), its direction. */
export interface ParetoAxisView {
  readonly name: string;
  readonly unit: string;
  readonly direction: 'higher' | 'lower';
}

/** One measured point; `time` is in minutes from the run's start. */
export interface ParetoPointView {
  readonly id: string;
  readonly label: string;
  readonly x: number;
  readonly y: number;
  readonly time: number;
  readonly state: 'input' | 'passed' | 'failed';
}

export interface ParetoView {
  readonly label: string;
  readonly points: readonly ParetoPointView[];
  readonly x: ParetoAxisView;
  readonly y: ParetoAxisView;
  /** The time bar's position in minutes; undefined at the live end, where every point is shown. */
  readonly cursor: number | undefined;
  /** The ideas to ring: the one open, and those on the chain the reader points at. */
  readonly highlighted: readonly string[];
}

/** The objectives the run's overview declares, in order; empty before it does. */
export function objectivesOf(model: RunModel): readonly ObjectiveAxis[] {
  return (model.overview?.objective.objectives ?? []).map(axis => ({
    name: axis.name,
    direction: axis.direction,
  }));
}

function axisView(name: string, unit: string | null, direction: 'max' | 'min'): ParetoAxisView {
  return {
    name: metricLabel(name),
    unit: unit ?? '',
    direction: direction === 'max' ? 'higher' : 'lower',
  };
}

const WITHOUT: readonly string[] = [];

/**
 * The trade-off chart of `model`, which is the whole run to its live end (never the run at a
 * scrubbed sequence, so scrubbing does not rescale it), or null unless the run has exactly two
 * objectives and a settled idea measured on both. `start` is the run's start on its clock; `scrub`
 * is the time bar's position as run-clock seconds (null at the live end: every point is shown);
 * `focus` the open idea and `lit` the ideas on the chain the reader points at.
 */
export function paretoView(
  model: RunModel,
  start: number,
  scrub: number | null,
  focus: string | null = null,
  lit: ReadonlySet<string> = new Set(),
): ParetoView | null {
  const found = tradeoffOf(model.candidates.candidates, objectivesOf(model));
  if (found === null) return null;
  const minutes = (at: number): number => (at - start) / 60;
  const [x, y] = found.axes;
  const input =
    found.input === null
      ? []
      : [
          {
            id: 'input',
            label: 'Input',
            x: found.input.x,
            y: found.input.y,
            time: 0,
            state: 'input' as const,
          },
        ];
  const points = [
    ...input,
    ...found.points.map(point => ({
      id: point.id,
      label: point.title,
      x: point.x,
      y: point.y,
      time: minutes(point.at),
      state: point.outcome,
    })),
  ];
  const ringed = [...lit, ...(focus === null || lit.has(focus) ? WITHOUT : [focus])];
  return {
    label: `${metricLabel(x.name)} and ${metricLabel(y.name).toLowerCase()} of each measured idea over the run, with the best trade-offs so far`,
    points,
    x: axisView(x.name, x.unit, x.direction),
    y: axisView(y.name, y.unit, y.direction),
    cursor: scrub === null ? undefined : minutes(scrub),
    highlighted: ringed,
  };
}
