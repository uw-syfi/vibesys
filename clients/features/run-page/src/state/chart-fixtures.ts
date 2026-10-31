/** The first objective's chart of a run model, for the tests that read one objective. */

import {type ObjectiveChartView, objectiveCharts} from './objective-chart.js';
import type {ObjectiveSeries} from './objective-reading.js';
import type {RunModel} from './run-model.js';

/** `objectiveCharts` of `whole` at the time bar's `scrub`, its first chart; the run has an objective, so it has one. */
export function firstChart(
  whole: RunModel,
  start: number,
  now: number | null,
  ...rest: [
    scrub?: number | null,
    focus?: string | null,
    lit?: ReadonlySet<string>,
    objectives?: readonly ObjectiveSeries[],
  ]
): ObjectiveChartView {
  const [scrub = null, focus = null, lit, objectives] = rest;
  const [chart] = objectiveCharts(
    whole,
    {start, now, scrub, focus, ...(lit === undefined ? {} : {lit})},
    objectives,
  );
  if (chart === undefined) throw new Error('the run has no objective to chart');
  return chart;
}
