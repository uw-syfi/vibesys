/**
 * The Overview tab as a selector over (the folded run at the view, `RunPageState`): the objective,
 * the key numbers, the performance chart and the current work. Measurements, counts and work are
 * queries over the run's spans (`@vibesys/run-state/overview`); there is no second record of them.
 * A run whose search labels no spans shows its objective and elapsed time, and its open flows as
 * its current work.
 */
import type {Span} from '@vibesys/contracts';
import {
  bestVsBaseline,
  type GateReading,
  gateTrajectory,
  runningBest,
  workstreamCounts,
} from '@vibesys/run-state/overview';
import {type ObjectiveBlock, objectiveBlocks} from './objective-text.js';
import {type RunAtView, spansAtView} from './run-at-view.js';
import {type RunPageState, runPhase} from './run-page.js';
import {
  duration,
  earliestTime,
  indexSpans,
  isOpen,
  kindOf,
  latestTime,
  nearest,
  numberAttr,
  ratio,
  type SpanIndex,
  stringAttr,
  titleOf,
  words,
} from './span-reading.js';
import type {Tone} from './views.js';

export interface ObjectiveView {
  readonly blocks: readonly ObjectiveBlock[];
  /** What the run is judged on, as one line: metric, unit and direction. */
  readonly metric: string | null;
  /** The correctness check a candidate must pass, as its command line. */
  readonly check: string | null;
  /** The evaluator package and version, when the task pins one. */
  readonly evaluator: string | null;
}

export interface KeyNumber {
  readonly label: string;
  readonly value: string;
  readonly sub: string;
}

export interface ChartPointView {
  /** Minutes since the run's first span. */
  readonly x: number;
  readonly y: number;
  /** The measured candidate was kept (its workstream accepted, or its gate passed). */
  readonly filled: boolean;
}

export interface ChartView {
  readonly label: string;
  readonly points: readonly ChartPointView[];
  readonly steps: readonly {readonly x: number; readonly y: number}[];
  readonly reference: {readonly y: number; readonly label: string} | null;
  /** Where the view is, in minutes since the run's first span. */
  readonly playhead: number;
  readonly xTicks: readonly {readonly value: number; readonly label: string}[];
  readonly yTicks: readonly {readonly value: number; readonly label: string}[];
  readonly xTitle: string;
  readonly legend: string;
}

export interface CurrentWorkItem {
  readonly id: string;
  readonly title: string;
  readonly doing: string;
  readonly tone: Extract<Tone, 'live' | 'warn' | 'neutral'>;
  readonly state: string;
}

export interface OverviewView {
  readonly objective: ObjectiveView | null;
  readonly numbers: readonly KeyNumber[];
  /** Null until a measurement has a value. */
  readonly chart: ChartView | null;
  readonly current: readonly CurrentWorkItem[];
}

function objectiveView(run: RunAtView): ObjectiveView | null {
  const overview = run.overview;
  if (overview === null) return null;
  const {objective} = overview;
  const direction =
    objective.direction === 'max'
      ? 'higher is better'
      : objective.direction === 'min'
        ? 'lower is better'
        : null;
  const metricParts = [
    objective.metric ?? null,
    objective.unit === undefined || objective.unit === null ? null : `in ${objective.unit}`,
    direction,
  ].filter((part): part is string => part !== null && part !== '');
  const check = objective.correctness_check ?? [];
  const evaluator = overview.benchmarks?.evaluator ?? null;
  return {
    blocks: objectiveBlocks(objective.text),
    metric: objective.metric ? metricParts.join(' · ') : null,
    check: check.length === 0 ? null : check.join(' '),
    evaluator: evaluator === null ? null : `${evaluator.name} ${evaluator.version}`,
  };
}

function bestNumber(run: RunAtView, spans: readonly Span[], ended: boolean): KeyNumber {
  const best = bestVsBaseline(spans, run.overview?.objective.direction);
  if (best === null) {
    return {label: 'Best vs baseline', value: '–', sub: 'no candidate has passed yet'};
  }
  const unit = run.overview?.objective.unit ?? null;
  const measured =
    best.metricValue === null ? '' : `${formatValue(best.metricValue)}${unit ? ` ${unit}` : ''}`;
  const when = ended ? 'final' : 'so far';
  return {
    label: 'Best vs baseline',
    value: best.vsBaseline === null ? measured || '–' : ratio(best.vsBaseline),
    sub: best.vsBaseline === null || measured === '' ? when : `${measured} · ${when}`,
  };
}

function roundNumber(index: SpanIndex): KeyNumber {
  const calls = index.spans.filter(span => kindOf(span) === 'planning_call');
  if (calls.length === 0) return {label: 'Round', value: '–', sub: 'no planning call yet'};
  const latest = calls.reduce(
    (best, span) => Math.max(best, numberAttr(span, 'call') ?? 0),
    calls.length,
  );
  const planning = calls.some(isOpen);
  const measuring = index.spans.some(span => kindOf(span) === 'gate' && isOpen(span));
  const sub = planning ? 'planning' : measuring ? 'measuring' : 'working';
  return {label: 'Round', value: String(latest), sub};
}

function workstreamsNumber(spans: readonly Span[]): KeyNumber {
  const counts = workstreamCounts(spans);
  return {
    label: 'Workstreams',
    value: String(counts.total),
    sub: `${counts.live} live · ${counts.waiting} waiting`,
  };
}

function elapsedNumber(spans: readonly Span[], ended: boolean): KeyNumber {
  const start = earliestTime(spans);
  const end = latestTime(spans);
  if (start === null || end === null) return {label: 'Elapsed', value: '–', sub: 'not started'};
  return {
    label: 'Elapsed',
    value: duration(end - start),
    sub: ended ? 'to the last step' : 'at the last step',
  };
}

/** A metric value with at most three significant decimals, grouped. */
export function formatValue(value: number): string {
  const abs = Math.abs(value);
  const digits = abs >= 100 ? 0 : abs >= 10 ? 1 : abs >= 1 ? 2 : 3;
  return value.toLocaleString('en-US', {maximumFractionDigits: digits});
}

/** About five round ticks over `[low, high]`. */
function ticks(low: number, high: number, label: (value: number) => string) {
  const span = high - low || Math.abs(high) || 1;
  const raw = span / 4;
  const magnitude = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map(m => m * magnitude).find(s => s >= raw) ?? raw;
  const first = Math.ceil(low / step) * step;
  const found: {value: number; label: string}[] = [];
  for (let value = first; value <= high + step / 1e6 && found.length < 12; value += step) {
    const rounded = Number(value.toPrecision(12));
    found.push({value: rounded, label: label(rounded)});
  }
  return found;
}

function kept(index: SpanIndex, reading: GateReading): boolean {
  const gate = index.byId.get(reading.span);
  const workstream = gate === undefined ? null : nearest(index, gate, 'workstream');
  const verdict = workstream?.label?.verdict;
  if (verdict) return verdict === 'accepted';
  return reading.verdict === 'passed';
}

function chartView(run: RunAtView, index: SpanIndex): ChartView | null {
  const start = earliestTime(index.spans) ?? 0;
  const minutes = (seconds: number) => (seconds - start) / 60;
  const measured = gateTrajectory(index.spans).filter(reading => reading.metricValue !== null);
  if (measured.length === 0) return null;
  const objective = run.overview?.objective;
  const unit = objective?.unit ?? null;
  const metric = objective?.metric ?? 'metric';
  const points = measured.map(reading => ({
    x: minutes(reading.at),
    y: reading.metricValue as number,
    filled: kept(index, reading),
  }));
  const playhead = minutes(latestTime(index.spans) ?? start);
  const best = runningBest(index.spans, objective?.direction).map(reading => ({
    x: minutes(reading.at),
    y: reading.metricValue as number,
  }));
  const last = best.at(-1);
  const steps = last === undefined ? [] : [...best, {x: Math.max(playhead, last.x), y: last.y}];
  const baselines = measured
    .filter(reading => reading.vsBaseline !== null && reading.vsBaseline !== 0)
    .map(reading => (reading.metricValue as number) / (reading.vsBaseline as number));
  const baseline = baselines[0] ?? null;
  const ys = [...points.map(point => point.y), ...(baseline === null ? [] : [baseline])];
  const unitText = unit ? ` ${unit}` : '';
  return {
    label: `${metric} of each measured candidate over the run, with the best so far${
      baseline === null ? '' : ' and the baseline'
    }`,
    points,
    steps,
    reference: baseline === null ? null : {y: baseline, label: 'baseline'},
    playhead,
    xTicks: ticks(0, Math.max(playhead, ...points.map(point => point.x)), value =>
      duration(value * 60),
    ),
    yTicks: ticks(Math.min(...ys), Math.max(...ys), formatValue),
    xTitle: 'time since the run started →',
    legend: `${metric}${unitText ? ` in${unitText}` : ''} · ● kept · ○ not kept · - - baseline · ┊ view`,
  };
}

/** What the deepest open span under `span` says it is doing. */
function doing(index: SpanIndex, span: Span, now: number | null): string {
  const open = deepestOpen(index, span) ?? span;
  const since = open.waiting_on?.[0]?.since ?? open.started_at;
  const age = now === null ? '' : `, ${duration(now - since)}`;
  const wait = open.waiting_on?.[0];
  if (open.status === 'waiting' && wait !== undefined) {
    const what: Record<string, string> = {
      slot: 'Waiting for a free slot',
      time: 'Waiting for its time',
      stop: 'Stopping',
      operation: 'Waiting on an operation',
    };
    return `${what[wait.kind] ?? `Waiting on ${words(wait.kind)}`}${age}`;
  }
  if (open.status === 'pending') return 'Not started';
  switch (kindOf(open)) {
    case 'agent_turn': {
      const role = stringAttr(open, 'role') ?? titleOf(open);
      return `${role.charAt(0).toUpperCase()}${role.slice(1)} turn${age}`;
    }
    case 'gate':
      return `Measuring ${open.label?.title ? titleOf(open) : words(stringAttr(open, 'stage') ?? open.name)}${age}`;
    case 'planning_call':
      return `Planning${age}`;
    default:
      return open === span ? `${words(open.status)}${age}` : `${titleOf(open)}${age}`;
  }
}

/** The latest-started open child of `span`, followed down to the deepest open span. */
function deepestOpen(index: SpanIndex, span: Span): Span | null {
  const child = (index.children.get(span.id) ?? []).filter(isOpen).at(-1);
  return child === undefined ? null : (deepestOpen(index, child) ?? child);
}

function currentItem(index: SpanIndex, span: Span, now: number | null): CurrentWorkItem {
  const hypothesis = stringAttr(span, 'hypothesis_id');
  const title = titleOf(span);
  const waiting = span.status === 'waiting' || span.status === 'pending';
  return {
    id: span.id,
    title: hypothesis !== null && hypothesis !== title ? `${hypothesis} · ${title}` : title,
    doing: doing(index, span, now),
    tone: waiting ? 'warn' : 'live',
    state: waiting ? 'Waiting' : 'Running',
  };
}

const CURRENT_KINDS: ReadonlySet<string | null> = new Set(['planning_call', 'workstream']);

/**
 * The open planning calls and workstreams; a run whose search labels no span shows its open flows
 * below the root instead.
 */
function currentWork(index: SpanIndex): readonly CurrentWorkItem[] {
  const now = latestTime(index.spans);
  const labelled = index.spans.some(span => kindOf(span) !== null);
  const open = index.spans.filter(span =>
    labelled
      ? CURRENT_KINDS.has(kindOf(span)) && isOpen(span)
      : span.type === 'flow' && span.parent !== null && isOpen(span),
  );
  return open.map(span => currentItem(index, span, now));
}

export function overviewView(run: RunAtView, state: RunPageState): OverviewView {
  const spans = spansAtView(run);
  const index = indexSpans(spans);
  const ended = runPhase(state).kind === 'ended';
  return {
    objective: objectiveView(run),
    numbers: [
      bestNumber(run, spans, ended),
      roundNumber(index),
      workstreamsNumber(spans),
      elapsedNumber(spans, ended),
    ],
    chart: chartView(run, index),
    current: currentWork(index),
  };
}
