/**
 * What a running idea is doing, in the words its Ideas row, its pane and its Logs pane say it
 * (`idea-activity.ts` folds the facts). Apart from `idea-words.ts`, which the Ideas fold reads:
 * these words name steps (`names.ts`), and the step names read the run page's views.
 */
import type {IdeaActivity} from './idea-activity.js';
import {stepLabel, stepTitle} from './names.js';
import {duration} from './span-reading.js';

/** Run-clock seconds without news after which a running idea reads as quiet. */
export const QUIET_AFTER = 120;

/**
 * What a running idea is doing, as its row and its pane say it: who and what (`Implementer · Using
 * Edit`, `Implementer · Profile the queue` for the todo it works on, `Implementer · Working` before
 * the run said more), `Measuring · correctness + benchmark`, `Waiting after Implementer`, `Starting`.
 * A tool is the agent's own name for it, shown as given.
 */
export function activityWords(activity: IdeaActivity): string {
  const {step} = activity;
  switch (activity.kind) {
    case 'turn': {
      const what = activity.tool === null ? (activity.todo ?? 'Working') : `Using ${activity.tool}`;
      return step === null ? what : `${stepLabel(step)} · ${what}`;
    }
    case 'gate': {
      const title = step === null ? '' : stepTitle(step);
      return title === '' ? 'Measuring' : `Measuring · ${title}`;
    }
    case 'waiting':
      return step === null ? 'Waiting' : `Waiting after ${stepLabel(step)}`;
    case 'starting':
      return 'Starting';
  }
}

/**
 * How long the activity has gone without news, to `now` (the run's newest news, `activityNow`):
 * `12s ago`, `quiet for 6m` once `QUIET_AFTER` has passed, or a gate's `running 1m` (a gate
 * measures for as long as its benchmark takes). Null when the run clock is unknown.
 */
export function activityAge(
  activity: IdeaActivity,
  now: number | null,
): {readonly text: string; readonly quiet: boolean} | null {
  if (now === null) return null;
  const age = Math.max(0, now - activity.since);
  if (activity.kind === 'gate') return {text: `running ${duration(age)}`, quiet: false};
  return age >= QUIET_AFTER
    ? {text: `quiet for ${duration(age)}`, quiet: true}
    : {text: `${duration(age)} ago`, quiet: false};
}

/** The activity and its age as one line: `Implementer · Using Edit · 12s ago`. */
export function activityLine(activity: IdeaActivity, now: number | null): string {
  const age = activityAge(activity, now);
  return age === null ? activityWords(activity) : `${activityWords(activity)} · ${age.text}`;
}

/** The idea Logs pane while the idea runs and has no log stream yet. */
export const LIVE_LOGS = {
  title: 'No log output yet',
  gate: 'Gate output appears here when measuring starts.',
  turn: "The agent's live output is in its current step.",
  open: 'Open live output',
} as const;
