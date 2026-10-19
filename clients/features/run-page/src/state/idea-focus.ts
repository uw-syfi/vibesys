/**
 * The performance row's reading of the open idea. The row (the hero and its chart) stays on screen
 * at every level; what the route opens only decides which idea it singles out: `openIdeaId` names
 * it (an idea open, or the idea a step or item sits under), `ideaFocusView` is the one line under
 * the hero's figure that tells its result, and the chart marks its point (`stripChartView`'s
 * `focus`).
 */
import type {BaseComparison, Candidate} from '@vibesys/run-state/candidates';
import {baseWords} from './base-words.js';
import {type Measure, resultLine, selectedWords, standingOf} from './idea-words.js';
import {resolveZoom} from './run-level.js';
import type {RunModel} from './run-model.js';
import {formatValue, ratio} from './span-reading.js';
import type {Route} from './zoom.js';

/** The id of the idea `route` opens in `model`, or null (the run, an agent, an id the run lacks). */
export function openIdeaId(model: RunModel, route: Route): string | null {
  return resolveZoom(model, route).idea?.id ?? null;
}

/** The open idea's result as one line under the hero's figure. */
export interface IdeaFocusView {
  readonly title: string;
  /** Its ratio to the input (`3.83×`), or with none to its base; null when unmeasured, and for a failed idea. */
  readonly ratio: string | null;
  /** What `ratio` is against: the base's name (`round 2`) when the run has no ratio to the input; null: the input. */
  readonly base: string | null;
  /** Its measured value with the unit; null when unmeasured, and for a failed idea. */
  readonly value: string | null;
  /** Its status and why in words (`Failed · not measured`, `Running`), read when no multiple shows. */
  readonly outcome: string;
  readonly tone: 'ok' | 'err' | 'muted';
}

function toneOf(idea: Candidate): IdeaFocusView['tone'] {
  if (idea.outcome === 'passed') return 'ok';
  return idea.outcome === 'failed' ? 'err' : 'muted';
}

/** What the idea measured against the input; null when it measured nothing. */
function measureOf(model: RunModel, idea: Candidate): Measure | null {
  const measurement = idea.measurement;
  const value = measurement?.metricValue ?? null;
  const vs = measurement?.vsBaseline ?? null;
  const against = measurement?.against ?? null;
  if (value === null || (vs === null && against === null)) return null;
  const unit = model.overview?.objective.unit;
  const words = `${formatValue(value)}${typeof unit === 'string' && unit !== '' ? ` ${unit}` : ''}`;
  if (vs !== null) return {ratio: ratio(vs), value: words, base: null};
  return {
    ratio: ratio((against as BaseComparison).ratio),
    value: words,
    base: baseWords(model.candidates.candidates, against as BaseComparison),
  };
}

/** The line for the idea `id`, or null when none is open or the run does not hold it. */
export function ideaFocusView(model: RunModel, id: string | null): IdeaFocusView | null {
  const idea = id === null ? undefined : model.candidates.candidates.find(item => item.id === id);
  if (idea === undefined) return null;
  const measure = measureOf(model, idea);
  // A failed idea's multiple is not its result: the line says it failed (`selectedWords`).
  const shown = idea.outcome === 'failed' ? null : measure;
  return {
    title: idea.title,
    ratio: shown?.ratio ?? null,
    base: shown?.base ?? null,
    value: shown?.value ?? null,
    outcome: selectedWords(idea, measure !== null),
    tone: toneOf(idea),
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
