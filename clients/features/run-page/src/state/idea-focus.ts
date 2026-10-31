/**
 * The performance row's reading of the open idea. The row (the hero and its charts) stays on screen
 * at every level; what the route opens only decides which idea it singles out: `openIdeaId` names
 * it (an idea open, or the idea a step or item sits under), and the charts ring its reading
 * (`objectiveCharts`' `focus`). `ideaOutcomeLine` tells its result in one line.
 */
import type {BaseComparison, Candidate} from '@vibesys/run-state/candidates';
import {baseWords} from './base-words.js';
import {type Measure, resultLine, standingOf} from './idea-words.js';
import {objectiveUnit} from './metric-words.js';
import {vsInputOf} from './objective-reading.js';
import {resolveZoom} from './run-level.js';
import type {RunModel} from './run-model.js';
import {formatValue, ratio} from './span-reading.js';
import type {Route} from './zoom.js';

/** The id of the idea `route` opens in `model`, or null (the run, an agent, an id the run lacks). */
export function openIdeaId(model: RunModel, route: Route): string | null {
  return resolveZoom(model, route).idea?.id ?? null;
}

/** What the idea measured against the input; null when it measured nothing. */
function measureOf(model: RunModel, idea: Candidate): Measure | null {
  if (model.loadFailure || (model.progress && model.progress.phase !== 'complete')) return null;
  const measurement = idea.measurement;
  const value = measurement?.metricValue ?? null;
  const vs = vsInputOf(model)(idea) ?? measurement?.vsBaseline ?? null;
  const against = measurement?.against ?? null;
  if (value === null || (vs === null && against === null)) return null;
  const unit = objectiveUnit(model.overview);
  const words = `${formatValue(value)}${unit === '' ? '' : ` ${unit}`}`;
  if (vs !== null) return {ratio: ratio(vs), value: words, base: null};
  return {
    ratio: ratio((against as BaseComparison).ratio),
    value: words,
    base: baseWords(model.candidates.candidates, against as BaseComparison),
  };
}

/**
 * The open idea's result as one line: `Passed · 3.97× the input (5,739,182 ops/s)`, `Adopted ·
 * passed · 1.90× the input (…)` for the run's leader, `Implementation failed`, or, for a failed idea
 * that was measured, `Failed its checks · measured 1.50× the input (…)`.
 */
export function ideaOutcomeLine(model: RunModel, idea: Candidate): string {
  return resultLine(idea, standingOf(model.candidates.best, idea.id), measureOf(model, idea));
}
