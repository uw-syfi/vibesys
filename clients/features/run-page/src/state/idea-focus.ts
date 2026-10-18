/**
 * The performance row's reading of the open idea. The row (the hero and its chart) stays on screen
 * at every level; what the route opens only decides which idea it singles out: `openIdeaId` names
 * it (an idea open, or the idea a step or item sits under), `ideaFocusView` is the one line under
 * the hero's figure that tells its result, and the chart marks its point (`stripChartView`'s
 * `focus`).
 */
import type {Candidate} from '@vibesys/run-state/candidates';
import {outcomeWords} from './ideas-view.js';
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
  /** Its ratio to the input (`3.83×`); null when unmeasured. */
  readonly ratio: string | null;
  /** Its measured value with the unit; null when unmeasured. */
  readonly value: string | null;
  /** Its outcome in words (`failed · not measured`, `running`), read when nothing was measured. */
  readonly outcome: string;
  readonly tone: 'ok' | 'err' | 'muted';
}

function toneOf(idea: Candidate): IdeaFocusView['tone'] {
  if (idea.outcome === 'passed') return 'ok';
  return idea.outcome === 'failed' ? 'err' : 'muted';
}

/** The line for the idea `id`, or null when none is open or the run does not hold it. */
export function ideaFocusView(model: RunModel, id: string | null): IdeaFocusView | null {
  const idea = id === null ? undefined : model.candidates.candidates.find(item => item.id === id);
  if (idea === undefined) return null;
  const measurement = idea.measurement;
  const value = measurement?.metricValue ?? null;
  const unit = model.overview?.objective.unit;
  const vs = measurement?.vsBaseline ?? null;
  return {
    title: idea.title,
    ratio: value === null || vs === null ? null : ratio(vs),
    value:
      value === null
        ? null
        : `${formatValue(value)}${typeof unit === 'string' && unit !== '' ? ` ${unit}` : ''}`,
    outcome: outcomeWords(idea),
    tone: toneOf(idea),
  };
}

/**
 * The idea's outcome as a sentence with its multiple: `Passed · 3.97× the input (5,739,182 ops/s)`,
 * `Failed · 0.62× the input`, or the bare outcome words (`failed · not measured`, `running`) when
 * nothing was measured.
 */
export function ideaOutcomeLine(model: RunModel, idea: Candidate): string {
  const view = ideaFocusView(model, idea.id);
  const words = outcomeWords(idea);
  const head = words.charAt(0).toUpperCase() + words.slice(1);
  if (view === null || view.ratio === null) {
    return idea.outcome === 'failed' && words === 'failed' ? `${head} · not measured` : head;
  }
  const value = view.value === null ? '' : ` (${view.value})`;
  return `${head} · ${view.ratio} the input${value}`;
}
