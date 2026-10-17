/**
 * The run level's hero (the best ratio to the input, its value, the input's, and the strip chart
 * of every measured idea with the running best), its Now pane (what runs now, each a shortcut to
 * its live step), and what a route resolves to in the run (`resolveZoom`): an id the run does not
 * hold resolves a level up, so a stale address still opens.
 */
import type {Span} from '@vibesys/contracts';
import type {AgentNode} from '@vibesys/run-state/agents';
import type {Candidate, CandidateStep} from '@vibesys/run-state/candidates';
import type {Direction, RunModel} from './run-model.js';
import {duration, formatValue, ratio} from './span-reading.js';
import type {Route} from './zoom.js';

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

/** The input's value: any measurement's value over its ratio to the input. */
function inputValue(model: RunModel): number | null {
  for (const item of model.candidates.candidates) {
    const value = item.measurement?.metricValue ?? null;
    const vs = item.measurement?.vsBaseline ?? null;
    if (value !== null && vs !== null && vs !== 0) return value / vs;
  }
  return null;
}

export function heroView(model: RunModel): HeroView {
  const metric = model.overview?.objective.metric;
  const best = bestCandidate(model)?.measurement ?? null;
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
    readonly faint: boolean;
  }[];
  readonly steps: readonly {readonly x: number; readonly y: number}[];
  readonly reference: {readonly y: number; readonly label: string} | null;
  readonly xTicks: readonly {readonly value: number; readonly label: string}[];
  readonly yTicks: readonly {readonly value: number; readonly label: string}[];
  readonly playhead: number | null;
}

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

function better(a: number, b: number, direction: Direction): boolean {
  return direction === 'min' ? a < b : a > b;
}

/**
 * Every measured idea as a point on the run clock (minutes from the run's start), the winning
 * path's filled and the rejected faint, with the running best of the kept as a step line.
 */
export function stripChartView(
  model: RunModel,
  start: number,
  now: number | null,
): StripChartView | null {
  const path = new Set(model.candidates.bestPath);
  const minutes = (at: number) => (at - start) / 60;
  const measured = model.candidates.candidates
    .filter(item => item.measurement?.metricValue != null)
    .sort((a, b) => (a.measurement?.at ?? 0) - (b.measurement?.at ?? 0));
  if (measured.length === 0) return null;
  const points = measured.map(item => ({
    id: item.id,
    label: item.title,
    x: minutes(item.measurement?.at ?? item.startedAt),
    y: item.measurement?.metricValue as number,
    filled: path.has(item.id),
    faint: item.outcome === 'rejected',
  }));
  const steps: {x: number; y: number}[] = [];
  for (const [index, item] of measured.entries()) {
    const point = points[index];
    if (item.outcome !== 'kept' || point === undefined) continue;
    const last = steps.at(-1);
    if (last === undefined || better(point.y, last.y, model.direction))
      steps.push({x: point.x, y: point.y});
  }
  const input = inputValue(model);
  const ys = [...points.map(point => point.y), ...(input === null ? [] : [input])];
  const end = Math.max(now === null ? 0 : minutes(now), ...points.map(point => point.x));
  const metric = model.overview?.objective.metric;
  return {
    label: `${typeof metric === 'string' ? metric : 'Metric'} of each measured idea over the run, with the best so far`,
    points,
    steps,
    reference: input === null ? null : {y: input, label: 'input'},
    xTicks: ticks(0, end, value => duration(value * 60)),
    yTicks: ticks(Math.min(...ys), Math.max(...ys), formatValue),
    playhead: now === null ? null : minutes(now),
  };
}

/** One thing running now. */
export interface NowItem {
  readonly id: string;
  readonly title: string;
  /** `implementer · Agent turn`: who runs it and what. */
  readonly summary: string;
  readonly since: number;
  /** Where the row jumps to. */
  readonly open:
    | {readonly kind: 'step'; readonly idea: string; readonly step: string}
    | {readonly kind: 'idea'; readonly idea: string}
    | {readonly kind: 'agent'; readonly agent: string};
}

function stepSummary(step: CandidateStep): string {
  return step.role === null ? step.title : `${step.role} · ${step.title}`;
}

/** What runs now: each running idea's latest running step, then agents working outside ideas. */
export function nowView(model: RunModel): readonly NowItem[] {
  const running = model.candidates.candidates.filter(item => item.outcome === 'running');
  const attempts = new Set(running.flatMap(item => item.attempts.map(attempt => attempt.span)));
  return [
    ...running.map(ideaNow),
    ...model.agents.agents
      .filter(
        agent => agent.status === 'running' && !attempts.has(agent.activity.within?.span ?? ''),
      )
      .map(agentNow),
  ];
}

function ideaNow(item: Candidate): NowItem {
  const step = item.running.at(-1);
  if (step === undefined) {
    const open = {kind: 'idea', idea: item.id} as const;
    return {id: item.id, title: item.title, summary: 'starting', since: item.startedAt, open};
  }
  return {
    id: step.span,
    title: item.title,
    summary: stepSummary(step),
    since: step.startedAt,
    open: {kind: 'step', idea: item.id, step: step.span},
  };
}

function agentNow(agent: AgentNode): NowItem {
  const within = agent.activity.within;
  return {
    id: agent.activity.turn,
    title: agent.role,
    summary: within === null ? 'agent turn' : within.title,
    since: agent.activity.startedAt,
    open: {kind: 'agent', agent: agent.id},
  };
}

/** The run clock now: the live clock when the page carries one, else the latest the spans say. */
export function clockAt(model: RunModel, live: number | null): number | null {
  return live ?? model.latest;
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
