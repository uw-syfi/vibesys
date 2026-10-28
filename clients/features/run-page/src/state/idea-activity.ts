/**
 * What each running idea is doing now, folded from the run's records: its newest running step,
 * and for an agent turn the activity the run wrote on the turn's span (`AgentTurnAttrs.activity`:
 * the tool it runs, else the todo item it works on, and when that began). Between steps it waits
 * after the step it finished; before its first step it is starting.
 *
 * Every time is the run clock the records carry. How long an idea has been quiet is measured to
 * the run's newest news (`activityNow`): the screen's run clock, moved up to the newest activity
 * any turn wrote, so a run that keeps writing makes a silent idea read as quiet without the
 * reader's clock.
 */
import type {Candidate, CandidateStep} from '@vibesys/run-state/candidates';
import type {RunModel} from './run-model.js';
import {attrsOf} from './span-reading.js';

/** What one running idea is doing. */
export interface IdeaActivity {
  /** An agent turn or a gate runs, the idea waits between steps, or it has not begun a step. */
  readonly kind: 'turn' | 'gate' | 'waiting' | 'starting';
  /** The running step (`turn`, `gate`), the step it waits after (`waiting`); null while starting. */
  readonly step: CandidateStep | null;
  /** The tool the turn runs, as the agent named it; null between tool calls and for other kinds. */
  readonly tool: string | null;
  /** The todo item the turn works on, when it runs no tool and has one. */
  readonly todo: string | null;
  /** When this activity began, on the run clock. */
  readonly since: number;
}

interface WrittenActivity {
  readonly tool: string | null;
  readonly todo: string | null;
  readonly since: number;
}

function text(value: unknown): string | null {
  return typeof value === 'string' && value !== '' ? value : null;
}

/** The activity the run wrote on a turn's span; null when it wrote none (an older run). */
function written(model: RunModel, step: CandidateStep): WrittenActivity | null {
  const span = model.trace.spans.get(step.span);
  const activity = span === undefined ? undefined : attrsOf(span)['activity'];
  if (typeof activity !== 'object' || activity === null) return null;
  const fields = activity as Readonly<Record<string, unknown>>;
  const since = fields['since'];
  return {
    tool: text(fields['tool']),
    todo: text(fields['todo']),
    since: typeof since === 'number' && Number.isFinite(since) ? since : step.startedAt,
  };
}

function stepActivity(model: RunModel, step: CandidateStep): IdeaActivity {
  if (step.kind !== 'agent_turn') {
    return {kind: 'gate', step, tool: null, todo: null, since: step.startedAt};
  }
  const said = written(model, step);
  const tool = said?.tool ?? null;
  return {
    kind: 'turn',
    step,
    tool,
    todo: tool === null ? (said?.todo ?? null) : null,
    since: Math.max(step.startedAt, said?.since ?? step.startedAt),
  };
}

/** What `idea` is doing now; null unless it runs. */
export function ideaActivity(model: RunModel, idea: Candidate): IdeaActivity | null {
  if (idea.outcome !== 'running') return null;
  const step = idea.running.at(-1);
  if (step !== undefined) return stepActivity(model, step);
  const attempt = idea.attempts.filter(each => each.outcome === 'running').at(-1);
  const last = attempt?.steps.at(-1);
  if (attempt === undefined || last === undefined) {
    const since = attempt?.startedAt ?? idea.startedAt;
    return {kind: 'starting', step: null, tool: null, todo: null, since};
  }
  return {
    kind: 'waiting',
    step: last,
    tool: null,
    todo: null,
    since: last.endedAt ?? attempt.startedAt,
  };
}

/** Every running idea's activity, by idea id. */
export function ideaActivities(model: RunModel): ReadonlyMap<string, IdeaActivity> {
  const found = new Map<string, IdeaActivity>();
  for (const idea of model.candidates.candidates) {
    const activity = ideaActivity(model, idea);
    if (activity !== null) found.set(idea.id, activity);
  }
  return found;
}

/**
 * The run's newest news: `now` (the screen's run clock; null when unknown) moved up to the newest
 * time a running idea's activity began. Null when neither is known.
 */
export function activityNow(
  activities: ReadonlyMap<string, IdeaActivity>,
  now: number | null,
): number | null {
  let newest = now;
  for (const activity of activities.values()) {
    if (newest === null || activity.since > newest) newest = activity.since;
  }
  return newest;
}
