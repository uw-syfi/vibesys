/**
 * The Overview tab as a selector over (the folded run at the view, `RunPageState`): the objective,
 * the key numbers, the performance chart and the current work. Measurements, counts and work are
 * queries over the run's spans (`@vibesys/run-state/overview`); there is no second record of them.
 * A run whose search labels no spans shows its objective and elapsed time, and its open flows as
 * its current work.
 */
import type {Span} from '@vibesys/contracts';
import {
  adoption,
  bestVsBaseline,
  type GateReading,
  gateTrajectory,
  runningBest,
  workstreamCounts,
} from '@vibesys/run-state/overview';
import {type ObjectiveBlock, objectiveBlocks} from './objective-text.js';
import {clockNow, type RunAtView, spansAtView} from './run-at-view.js';
import {type RunPageState, runPhase} from './run-page.js';
import {
  duration,
  earliestTime,
  formatValue,
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
  upperFirst,
  words,
} from './span-reading.js';
import {openWaits, waitPhrase} from './span-words.js';
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

/** What a run is doing before its first planning call or workstream: a title and one line. */
export interface ProgressNote {
  readonly title: string;
  readonly body: string;
}

export interface OverviewView {
  /** Set while the run is starting or measuring, before it has planned or started any work. */
  readonly progress: ProgressNote | null;
  readonly objective: ObjectiveView | null;
  readonly numbers: readonly KeyNumber[];
  /** Null until a measurement has a value. */
  readonly chart: ChartView | null;
  readonly current: readonly CurrentWorkItem[];
}

/**
 * What an evaluator command's placeholders stand for when it runs, in words: the run substitutes
 * them per candidate (`${PROJECT_ROOT}` is the candidate's own checkout), so the command a person
 * reads says what they stand for instead of showing the unexpanded variable.
 */
const COMMAND_PLACEHOLDERS: readonly (readonly [string, string])[] = [
  ['${PROJECT_ROOT}', '<the candidate’s project>'],
  ['${PYTHON}', 'python'],
];

/** A command's argument vector as one line, its placeholders written as what they stand for. */
export function commandLine(argv: readonly string[]): string {
  return COMMAND_PLACEHOLDERS.reduce(
    (line, [token, words]) => line.replaceAll(token, words),
    argv.join(' '),
  );
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
    check: check.length === 0 ? null : commandLine(check),
    evaluator: evaluator === null ? null : `${evaluator.name} ${evaluator.version}`,
  };
}

/** A candidate's headline: its ratio to the input's (else its value) and what it measured. */
function headline(
  run: RunAtView,
  reading: {readonly metricValue: number | null; readonly vsBaseline: number | null},
  when: string,
): Pick<KeyNumber, 'value' | 'sub'> {
  const unit = run.overview?.objective.unit ?? null;
  const measured =
    reading.metricValue === null
      ? ''
      : `${formatValue(reading.metricValue)}${unit ? ` ${unit}` : ''}`;
  return {
    value: reading.vsBaseline === null ? measured || '–' : ratio(reading.vsBaseline),
    sub: reading.vsBaseline === null || measured === '' ? when : `${measured} · ${when}`,
  };
}

/**
 * The run's best candidate: the one it adopted, once it has (the search's own account). While the
 * run is live and has not adopted, the best passed gate so far; a run that ended without adopting
 * names no candidate.
 */
function bestNumber(run: RunAtView, spans: readonly Span[], ended: boolean): KeyNumber {
  const label = 'Best vs baseline';
  const adopted = adoption(spans);
  if (adopted?.verdict === 'adopted') {
    if (adopted.candidate === null) return {label, value: '–', sub: 'no candidate beat the input'};
    return {label, ...headline(run, adopted, 'adopted')};
  }
  if (ended) {
    const why = adopted?.verdict === 'not_adopted' ? 'the adoption failed' : 'no candidate adopted';
    return {label, value: '–', sub: why};
  }
  const best = bestVsBaseline(spans, run.overview?.objective.direction);
  if (best === null) return {label, value: '–', sub: 'no candidate has passed yet'};
  return {label, ...headline(run, best, 'so far')};
}

/**
 * The run's round: its latest planning call, or, for a search that plays rounds instead (each a
 * workstream with a `round` attribute), its latest round. A round still choosing its hypothesis
 * (no `hypothesis_id` yet) is planning.
 */
function roundNumber(index: SpanIndex, ended: boolean): KeyNumber {
  const calls = index.spans.filter(span => kindOf(span) === 'planning_call');
  const rounds = index.spans.filter(
    span => kindOf(span) === 'workstream' && numberAttr(span, 'round') !== null,
  );
  if (calls.length === 0 && rounds.length === 0) {
    return {label: 'Round', value: '–', sub: 'before the first plan'};
  }
  const latest =
    calls.length > 0
      ? calls.reduce((best, span) => Math.max(best, numberAttr(span, 'call') ?? 0), calls.length)
      : rounds.reduce((best, span) => Math.max(best, numberAttr(span, 'round') ?? 0), 0);
  const planning =
    calls.some(isOpen) ||
    rounds.some(span => isOpen(span) && stringAttr(span, 'hypothesis_id') === null);
  const measuring = index.spans.some(span => kindOf(span) === 'gate' && isOpen(span));
  const sub = ended ? 'finished' : planning ? 'planning' : measuring ? 'measuring' : 'working';
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
  if (start === null || end === null) return {label: 'Elapsed', value: '–', sub: 'no steps yet'};
  return {
    label: 'Elapsed',
    value: duration(end - start),
    sub: ended ? 'to the last step' : 'at the last step',
  };
}

/** A value on an axis, compact (`24M`, `1.5k`, `0.002`) so a long number never crowds the gutter. */
function formatAxisValue(value: number): string {
  return value.toLocaleString('en-US', {
    notation: 'compact',
    maximumFractionDigits: Math.abs(value) >= 1 ? 1 : 3,
  });
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
    yTicks: ticks(Math.min(...ys), Math.max(...ys), formatAxisValue),
    xTitle: 'time since the run started →',
    legend: `${metric}${unitText ? ` in${unitText}` : ''} · ● kept · ○ not kept · - - baseline · ┊ view`,
  };
}

/** What the deepest open span under `span` says it is doing. */
function doing(index: SpanIndex, span: Span, now: number | null): string {
  const open = deepestOpen(index, span) ?? span;
  const wait = openWaits(open)[0];
  const since = wait?.since ?? open.started_at;
  const age = now === null ? '' : `, ${duration(now - since)}`;
  if (open.status === 'waiting' && wait !== undefined) {
    const what = wait.kind === 'stop' ? 'Stopping' : `Waiting on ${waitPhrase(wait, index.byId)}`;
    return `${what}${age}`;
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
function currentWork(index: SpanIndex, now: number | null): readonly CurrentWorkItem[] {
  const labelled = index.spans.some(span => kindOf(span) !== null);
  const open = index.spans.filter(span =>
    labelled
      ? CURRENT_KINDS.has(kindOf(span)) && isOpen(span)
      : span.type === 'flow' && span.parent !== null && isOpen(span),
  );
  // Before the first plan or workstream, the run's own open steps (the input's measurement).
  const items = open.length === 0 && labelled ? openUnderRoot(index) : open;
  return items.map(span => currentItem(index, span, now));
}

/** The open spans directly under the run's root, oldest first. */
function openUnderRoot(index: SpanIndex): Span[] {
  const root = index.spans.find(span => span.parent === null);
  return root === undefined ? [] : (index.children.get(root.id) ?? []).filter(isOpen);
}

/**
 * What a starting or running run is doing while it has no planning call or workstream yet, from the
 * record's startup stage and the spans it has open (the input's measurement, with its gate's
 * state); null once it has planned or started work, or when it is not starting or running.
 */
export function progressNote(run: RunAtView, state: RunPageState): ProgressNote | null {
  const phase = runPhase(state).kind;
  if (phase !== 'starting' && phase !== 'running') return null;
  const index = indexSpans(spansAtView(run));
  if (index.spans.some(span => CURRENT_KINDS.has(kindOf(span)))) return null;
  const top = openUnderRoot(index)[0];
  if (top !== undefined) {
    return {
      title: upperFirst(words(titleOf(top))),
      body: upperFirst(doing(index, top, clockNow(state, index.spans))),
    };
  }
  if (phase === 'starting') {
    const status = state.record.kind === 'live' ? state.record.run.status : null;
    return {
      title: 'Starting the run',
      body: status === null ? 'Preparing the run environment.' : upperFirst(status),
    };
  }
  return {
    title: 'Waiting for the first step',
    body: 'The run has started but has not opened a step yet.',
  };
}

export function overviewView(run: RunAtView, state: RunPageState): OverviewView {
  const spans = spansAtView(run);
  const index = indexSpans(spans);
  const ended = runPhase(state).kind === 'ended';
  return {
    progress: progressNote(run, state),
    objective: objectiveView(run),
    numbers: [
      bestNumber(run, spans, ended),
      roundNumber(index, ended),
      workstreamsNumber(spans),
      elapsedNumber(spans, ended),
    ],
    chart: chartView(run, index),
    current: currentWork(index, clockNow(state, spans)),
  };
}
