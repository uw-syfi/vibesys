/**
 * Where the time bar stands on the run, as every face of the dashboard reads it. The charts, the
 * stats and the Ideas table are all drawn from the run to its end (`whole`), cut at the position,
 * and never from the run as it stood there: that run holds the writer's series without its
 * decisions, and a figure read from it would differ from the chart's at every position but the end.
 */
import type {RunModel} from './run-model.js';

/** The run to its end and the time bar's place on it. */
export interface RunPosition {
  /** The run to its end, whatever the sequence of the model being viewed. */
  readonly whole: RunModel;
  /** The bar's position in run-clock seconds; null at the run's end. */
  readonly scrub: number | null;
}

/**
 * The position of `model` on `whole`: the bar stands before the run's end exactly when the viewed
 * model is not the whole run. `start` is the run's start on its clock, the position of a viewed
 * model that has no time yet.
 */
export function positionOf(model: RunModel, whole: RunModel, start: number): RunPosition {
  const scrubbed = model.at !== null && model.at !== whole.at;
  return {whole, scrub: scrubbed ? (model.latest ?? start) : null};
}

/** The position of a run viewed at its end. */
export function endOf(whole: RunModel): RunPosition {
  return {whole, scrub: null};
}
