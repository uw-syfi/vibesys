/**
 * What each running idea is doing (the Ideas table words it under the idea's name), the run clock's
 * start and now, and what a route resolves to in the run (`resolveZoom`): an id the run does not
 * hold resolves a level up, so a stale address still opens.
 */
import {roleName, type Span} from '@vibesys/contracts';
import type {AgentNode} from '@vibesys/run-state/agents';
import type {Candidate, CandidateStep} from '@vibesys/run-state/candidates';
import {stepLabel, stepTitle} from './names.js';
import type {RunModel} from './run-model.js';
import {type RunPageState, runPhase} from './run-page.js';
import {type LogScope, streamToFollow} from './scoped-logs.js';
import {openWaits} from './span-words.js';
import {timelineView} from './views.js';
import {type Route, routeLevel} from './zoom.js';

/** What a running idea is doing, and since when on the run clock. */
export interface IdeaDoing {
  /**
   * What runs, kept whole on a narrow row: who runs it and what (`Builder · Agent turn`,
   * `Waiting after Builder`).
   */
  readonly doing: string;
  readonly since: number;
}

/** When the span began its newest wait on something other than a timer or the stop; null if none. */
function waitingSince(span: Span | undefined): number | null {
  if (span === undefined) return null;
  const since = openWaits(span)
    .filter(wait => wait.kind !== 'time' && wait.kind !== 'stop')
    .map(wait => wait.since);
  return since.length === 0 ? null : Math.max(...since);
}

/** What a step does: its role, and its title unless that only repeats the role (`implementer`). */
function stepSummary(step: CandidateStep): string {
  if (step.role === null) return step.title;
  const role = roleName(step.role);
  const title = stepTitle(step);
  return title.trim().toLowerCase() === role.trim().toLowerCase() || title === ''
    ? role
    : `${role} · ${title}`;
}

/** What each running idea is doing, by the idea's id. */
export function ideaDoing(model: RunModel): ReadonlyMap<string, IdeaDoing> {
  return new Map(
    model.candidates.candidates
      .filter(item => item.outcome === 'running')
      .map(item => [item.id, doing(item, model.trace.spans)]),
  );
}

/**
 * A running idea's newest running step; between steps (its agent yielded to an evaluation, say)
 * the step it last finished, waiting since the attempt began to wait; and before its first step,
 * starting since its running attempt began (a later attempt of a kept idea starts long after the
 * idea did).
 */
function doing(item: Candidate, spans: ReadonlyMap<string, Span>): IdeaDoing {
  const step = item.running.at(-1);
  if (step === undefined) {
    const attempt = item.attempts.filter(each => each.outcome === 'running').at(-1);
    const last = attempt?.steps.at(-1);
    if (attempt === undefined || last === undefined) {
      return {doing: 'Starting', since: attempt?.startedAt ?? item.startedAt};
    }
    return {
      doing: `Waiting after ${stepLabel(last)}`,
      since: waitingSince(spans.get(attempt.span)) ?? last.endedAt ?? attempt.startedAt,
    };
  }
  return {doing: stepSummary(step), since: step.startedAt};
}

/** The run clock now: the live clock when the page carries one, else the latest the spans say. */
export function clockAt(model: RunModel, live: number | null): number | null {
  return live ?? model.latest;
}

/** The run clock's start: the earliest span, independent of map serialization order. */
export function clockStart(model: RunModel): number {
  let start: number | null = null;
  for (const span of model.trace.spans.values()) {
    start = start === null ? span.started_at : Math.min(start, span.started_at);
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
  if (runPhase(state).kind === 'starting') {
    const active = timelineView(state).find(
      stage => stage.state === 'active' && stage.streamId !== null,
    )?.streamId;
    return active && active !== state.selected ? active : null;
  }
  const scope = shownLogScope(route, resolveZoom(model, route));
  return scope === null ? null : streamToFollow(state, scope);
}
