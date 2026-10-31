/** The first objective's chart of a run model, for the tests that read one objective. */
import type {ObjectiveChartView} from './objective-chart.js';
import {objectiveCharts} from './objective-chart.js';
import type {RunModel} from './run-model.js';

/** `objectiveCharts` of `whole`, its first chart; the run has an objective, so it has one. */
export function firstChart(
  whole: RunModel,
  ...rest: Parameters<typeof objectiveCharts> extends [unknown, ...infer Tail] ? Tail : never
): ObjectiveChartView {
  const [chart] = objectiveCharts(whole, ...rest);
  if (chart === undefined) throw new Error('the run has no objective to chart');
  return chart;
}
