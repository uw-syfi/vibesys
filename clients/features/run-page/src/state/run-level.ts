/**
 * The run level's hero (the best ratio to the input, its value, the input's, and the strip chart
 * of every measured idea with the running best), its Now pane (what runs now, each a shortcut to
 * its live step), and what a route resolves to in the run (`resolveZoom`): an id the run does not
 * hold resolves a level up, so a stale address still opens.
 */
import type {Span} from '@vibesys/contracts';
import type {AgentNode} from '@vibesys/run-state/agents';
import type {Candidate, CandidateStep} from '@vibesys/run-state/candidates';
import {
  type IdeaResult,
  inputValueOf,
  type PerformancePoint,
  performanceFold,
} from './performance.js';
import type {Direction, RunModel} from './run-model.js';
import type {RunPageState} from './run-page.js';
import {type LogScope, streamToFollow} from './scoped-logs.js';
import {duration, formatValue, ratio} from './span-reading.js';
import {openWaits} from './span-words.js';
import {type Route, routeLevel} from './zoom.js';

export interface HeroView {
  /** The metric and which way is better: `throughput ↑`. */
  readonly metric: string;
  /** The best's ratio to the input (`1.42×`); null before anything passed. */
  readonly ratio: string | null;
  /** The best's value with its unit. */
  readonly value: string | null;
  /** The input's value: `input 9,380,000`. */
  readonly input: string | null;
  readonly measured: number;
  /**
   * What the figure is, by the run's adoption span: `adopted` once the run ended, `best` while it
   * works; null when the run has no best (no adoption yet, or the input stands).
   */
  readonly standing: 'best' | 'adopted' | null;
  /** The idea the figure belongs to, to open it; null when `standing` is null. */
  readonly bestIdea: {readonly id: string; readonly title: string} | null;
}

function unitSuffix(model: RunModel): string {
  const unit = model.overview?.objective.unit;
  return typeof unit === 'string' && unit !== '' ? ` ${unit}` : '';
}

function arrow(direction: Direction): string {
  if (direction === 'max') return ' ↑';
  return direction === 'min' ? ' ↓' : '';
}

function bestCandidate(model: RunModel): Candidate | null {
  const id = model.candidates.best?.candidate ?? null;
  return model.candidates.candidates.find(item => item.id === id) ?? null;
}

function inputValue(model: RunModel): number | null {
  return inputValueOf(model.candidates.candidates);
}

export function heroView(model: RunModel): HeroView {
  const metric = model.overview?.objective.metric;
  const leader = bestCandidate(model);
  const best = leader?.measurement ?? null;
  const input = inputValue(model);
  const unit = unitSuffix(model);
  return {
    metric: `${typeof metric === 'string' && metric !== '' ? metric : 'metric'}${arrow(model.direction)}`,
    ratio: best?.vsBaseline === null || best === null ? null : ratio(best.vsBaseline),
    value:
      best?.metricValue === null || best === null
        ? null
        : `${formatValue(best.metricValue)}${unit}`,
    input: input === null ? null : `input ${formatValue(input)}`,
    measured: model.candidates.candidates.filter(item => item.measurement?.metricValue != null)
      .length,
    standing: leader === null ? null : model.candidates.best?.adopted === true ? 'adopted' : 'best',
    bestIdea: leader === null ? null : {id: leader.id, title: leader.title},
  };
}

export interface StripChartView {
  readonly label: string;
  readonly points: readonly {
    readonly id: string;
    readonly label: string;
    readonly x: number;
    readonly y: number;
    readonly filled: boolean;
    readonly tone: 'accent' | 'muted' | 'err';
    /** After the scrub position: drawn dimmed, not removed. */
    readonly faint: boolean;
    /**
     * With an idea open: its point is `focus`, its base idea's `related`, the rest `dim` (all
     * stay drawn); without one, or when the open idea has no point, the points are not singled out.
     */
    readonly emphasis?: 'focus' | 'related' | 'dim';
  }[];
  /** With an idea open: the stretch of the run clock it took, in minutes, as a band on the x-axis. */
  readonly spans: readonly {readonly from: number; readonly to: number; readonly label: string}[];
  /** The best passed value so far (the input's until an idea beats it), as a step line that reaches the end of the axis. */
  readonly steps: readonly {readonly x: number; readonly y: number}[];
  readonly reference: {readonly y: number; readonly label: string} | null;
  readonly xTicks: readonly {readonly value: number; readonly label: string}[];
  readonly yTicks: readonly {readonly value: number; readonly label: string}[];
  /** The y-axis range: from 0 to the largest value of the whole run, with headroom. */
  readonly yDomain: readonly [number, number];
  /** The cursor: the live clock now, or the scrub position when the reader looks at the past. */
  readonly playhead: number | null;
}

/** The share of the largest value left above it, so the top dot is not on the frame. */
const HEADROOM = 1.1;

/** Round tick positions over [low, high]: at most a handful, at 1, 2, 2.5 or 5 times a power of ten. */
function ticks(low: number, high: number, label: (value: number) => string) {
  const span = high - low || Math.abs(high) || 1;
  const raw = span / 4;
  const magnitude = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map(m => m * magnitude).find(s => s >= raw) ?? raw;
  const found: {value: number; label: string}[] = [];
  for (let value = Math.ceil(low / step) * step; value <= high + step / 1e6; value += step) {
    const rounded = Number(value.toPrecision(12));
    found.push({value: rounded, label: label(rounded)});
    if (found.length >= 8) break;
  }
  return found;
}

const RESULT_WORDS: Readonly<Record<IdeaResult, string>> = {
  passed: 'passed',
  failed: 'failed',
  pending: 'not measured yet',
};

function pointTone(point: PerformancePoint): 'accent' | 'muted' | 'err' {
  if (point.best) return 'accent';
  return point.result === 'failed' ? 'err' : 'muted';
}

/** The open idea's reading of the chart: how each dot is singled out, and the idea's time span. */
interface FocusReading {
  emphasisOf(id: string): 'focus' | 'related' | 'dim' | undefined;
  spans(minutes: (at: number) => number, reached: number | null): StripChartView['spans'];
}

const NO_FOCUS: FocusReading = {emphasisOf: () => undefined, spans: () => []};

/**
 * The idea `focus` names in `model` as the chart reads it: its dot is the focus, its base's dot is
 * related, the others dim (when the idea has a dot to single out); nothing when none is open.
 */
function focusOn(
  model: RunModel,
  points: readonly PerformancePoint[],
  focus: string | null,
): FocusReading {
  const open = focus === null ? undefined : model.candidates.candidates.find(i => i.id === focus);
  if (open === undefined) return NO_FOCUS;
  const dots = points.some(point => point.id === open.id);
  return {
    emphasisOf(id) {
      if (id === open.id) return 'focus';
      if (id === open.parent) return 'related';
      return dots ? 'dim' : undefined;
    },
    spans(minutes, reached) {
      const to = open.endedAt ?? reached ?? open.startedAt;
      return [
        {
          from: minutes(open.startedAt),
          to: minutes(to),
          label: `${open.title} · ${duration(to - open.startedAt)}`,
        },
      ];
    },
  };
}

function emphasisField(emphasis: 'focus' | 'related' | 'dim' | undefined): {
  readonly emphasis?: 'focus' | 'related' | 'dim';
} {
  return emphasis === undefined ? {} : {emphasis};
}

/**
 * Every measured idea as a point on the run clock (minutes from the run's start) over a y-axis
 * that starts at 0: the ideas that set a new best sit on the step line of the best so far, ideas
 * that passed without beating it are grey, ideas that failed are red (`performanceFold`).
 *
 * `model` is the whole run to its live end, never the run at a scrubbed sequence: what the chart
 * shows and the range of both axes depend on the run alone. `scrub` is the run-clock time the
 * reader scrubbed to (null at the live end); it is drawn as the cursor and dims the points after
 * it. `now` is the live clock, else the run's latest time bounds the x-axis.
 *
 * `focus` is the id of the idea the reader has open, or null. It only changes how the same points
 * are drawn (`emphasis`) and adds the idea's time span; the points, steps, ticks and both axes'
 * ranges are the same with or without it, so opening an idea never moves the chart.
 */
export function stripChartView(
  model: RunModel,
  start: number,
  now: number | null,
  scrub: number | null = null,
  focus: string | null = null,
): StripChartView | null {
  const minutes = (at: number) => (at - start) / 60;
  const fold = performanceFold(model.candidates.candidates, model.direction, inputValue(model));
  if (fold.points.length === 0) return null;
  const open = focusOn(model, fold.points, focus);
  const points = fold.points.map(point => ({
    id: point.id,
    label: `${point.title} · ${formatValue(point.value)} · ${point.best ? 'new best' : RESULT_WORDS[point.result]}`,
    x: minutes(point.at),
    y: point.value,
    filled: point.result !== 'pending',
    tone: pointTone(point),
    faint: scrub !== null && point.at > scrub,
    ...emphasisField(open.emphasisOf(point.id)),
  }));
  const reached = now ?? model.latest;
  const end = Math.max(reached === null ? 0 : minutes(reached), ...points.map(point => point.x));
  const steps = fold.best.map(step => ({x: minutes(step.at), y: step.value}));
  const last = steps.at(-1);
  if (last !== undefined && last.x < end) steps.push({x: end, y: last.y});
  const input = inputValue(model);
  const ys = [0, ...points.map(point => point.y), ...(input === null ? [] : [input])];
  const top = Math.max(...ys);
  const metric = model.overview?.objective.metric;
  return {
    label: `${typeof metric === 'string' ? metric : 'Metric'} of each measured idea over the run, with the best so far`,
    points,
    spans: open.spans(minutes, reached),
    steps,
    reference: input === null ? null : {y: input, label: 'input'},
    xTicks: ticks(0, end, value => duration(value * 60)),
    yTicks: ticks(Math.min(...ys), top, formatValue),
    yDomain: [Math.min(...ys), top > 0 ? top * HEADROOM : 1],
    playhead: scrub !== null ? minutes(scrub) : now === null ? null : minutes(now),
  };
}

/** One thing running now. */
export interface NowItem {
  readonly id: string;
  /**
   * What runs, kept whole on a narrow row: who runs it and what (`implementer · Agent turn`,
   * `waiting after implementer`) for an idea, the role for an agent.
   */
  readonly doing: string;
  /** What it belongs to, which gives way first: the idea's title, or what the agent works in. */
  readonly context: string;
  readonly since: number;
  /** Where the row jumps to. */
  readonly open:
    | {readonly kind: 'step'; readonly idea: string; readonly step: string}
    | {readonly kind: 'idea'; readonly idea: string}
    | {readonly kind: 'agent'; readonly agent: string};
}

/** When the span began its newest wait on something other than a timer or the stop; null if none. */
function waitingSince(span: Span | undefined): number | null {
  if (span === undefined) return null;
  const since = openWaits(span)
    .filter(wait => wait.kind !== 'time' && wait.kind !== 'stop')
    .map(wait => wait.since);
  return since.length === 0 ? null : Math.max(...since);
}

function stepSummary(step: CandidateStep): string {
  return step.role === null ? step.title : `${step.role} · ${step.title}`;
}

/** What runs now: each running idea's latest running step, then agents working outside ideas. */
export function nowView(model: RunModel): readonly NowItem[] {
  const running = model.candidates.candidates.filter(item => item.outcome === 'running');
  const attempts = new Set(running.flatMap(item => item.attempts.map(attempt => attempt.span)));
  return [
    ...running.map(item => ideaNow(item, model.trace.spans)),
    ...model.agents.agents
      .filter(
        agent => agent.status === 'running' && !attempts.has(agent.activity.within?.span ?? ''),
      )
      .map(agentNow),
  ];
}

/**
 * A running idea's row: its newest running step; between steps (its agent yielded to an
 * evaluation, say) the step it last finished, waiting since the attempt began to wait; and before
 * its first step, starting since its running attempt began (a later attempt of a kept idea starts
 * long after the idea did).
 */
function ideaNow(item: Candidate, spans: ReadonlyMap<string, Span>): NowItem {
  const step = item.running.at(-1);
  if (step === undefined) {
    const attempt = item.attempts.filter(each => each.outcome === 'running').at(-1);
    const last = attempt?.steps.at(-1);
    if (attempt === undefined || last === undefined) {
      const open = {kind: 'idea', idea: item.id} as const;
      const since = attempt?.startedAt ?? item.startedAt;
      return {id: item.id, doing: 'starting', context: item.title, since, open};
    }
    return {
      id: attempt.span,
      doing: `waiting after ${last.role ?? last.title}`,
      context: item.title,
      since: waitingSince(spans.get(attempt.span)) ?? last.endedAt ?? attempt.startedAt,
      open: {kind: 'step', idea: item.id, step: last.span},
    };
  }
  return {
    id: step.span,
    doing: stepSummary(step),
    context: item.title,
    since: step.startedAt,
    open: {kind: 'step', idea: item.id, step: step.span},
  };
}

function agentNow(agent: AgentNode): NowItem {
  const within = agent.activity.within;
  return {
    id: agent.activity.turn,
    doing: agent.role,
    context: within === null ? 'agent turn' : within.title,
    since: agent.activity.startedAt,
    open: {kind: 'agent', agent: agent.id},
  };
}

/** The run clock now: the live clock when the page carries one, else the latest the spans say. */
export function clockAt(model: RunModel, live: number | null): number | null {
  return live ?? model.latest;
}

/**
 * How far the run clock now (`live`, carried on this window's clock) is past the latest time the
 * spans carry: what has passed since the run last said anything. 0 without a live clock.
 */
export function clockCarried(model: RunModel, live: number | null): number {
  return live === null || model.latest === null ? 0 : Math.max(0, live - model.latest);
}

/** The run clock's start: the first span's start. */
export function clockStart(model: RunModel): number {
  let start: number | null = null;
  for (const span of model.trace.spans.values()) {
    start = span.started_at;
    break;
  }
  return start ?? 0;
}

/** What a route opens in the run, each level present only when the one above it is. */
export interface ResolvedZoom {
  readonly idea: Candidate | null;
  readonly agent: AgentNode | null;
  /** The step: one of the idea's steps, or a turn of the agent. */
  readonly step: CandidateStep | null;
  readonly item: string | null;
}

function ideaStep(idea: Candidate, span: string): CandidateStep | null {
  for (let at = idea.attempts.length - 1; at >= 0; at -= 1) {
    const found = idea.attempts[at]?.steps.find(step => step.span === span);
    if (found !== undefined) return found;
  }
  return null;
}

function turnStep(span: Span | undefined, agent: AgentNode): CandidateStep | null {
  if (span === undefined) return null;
  return {
    span: span.id,
    kind: span.label?.kind ?? 'agent_turn',
    title: span.label?.title || span.name,
    role: agent.role,
    status: span.status,
    startedAt: span.started_at,
    endedAt: span.ended_at ?? null,
  };
}

export function resolveZoom(model: RunModel, route: Route): ResolvedZoom {
  const idea =
    route.idea === null
      ? null
      : (model.candidates.candidates.find(item => item.id === route.idea) ?? null);
  const agent =
    idea !== null || route.agent === null
      ? null
      : (model.agents.agents.find(item => item.id === route.agent) ?? null);
  let step: CandidateStep | null = null;
  if (route.step !== null && idea !== null) step = ideaStep(idea, route.step);
  if (route.step !== null && agent !== null)
    step = turnStep(model.trace.spans.get(route.step), agent);
  return {idea, agent, step, item: step === null ? null : route.item};
}

/**
 * Whose logs the screen shows for `route`, or null when no Logs face is on screen: the run's at the
 * run level, an idea's at the idea, a step's at the step (under an idea or an agent). An agent and
 * an item have no Logs face. The shell follows the shown stream with it (`streamToFollow`).
 */
export function shownLogScope(route: Route, zoomed: ResolvedZoom): LogScope | null {
  if (route.mode !== 'logs') return null;
  if (routeLevel(route) === 'run' || (zoomed.idea === null && zoomed.agent === null)) {
    return {kind: 'run'};
  }
  if (zoomed.step !== null) return zoomed.item === null ? {kind: 'step', step: zoomed.step} : null;
  return zoomed.idea === null ? null : {kind: 'idea', idea: zoomed.idea};
}

/**
 * The stream the page must select so it fetches what the Logs face on screen shows, or null when
 * no Logs face is on screen or the page already follows it. The shell dispatches `select-stream`
 * with it; a view runs no effects.
 */
export function logStreamToFollow(
  state: RunPageState,
  model: RunModel,
  route: Route,
): string | null {
  const scope = shownLogScope(route, resolveZoom(model, route));
  return scope === null ? null : streamToFollow(state, scope);
}
