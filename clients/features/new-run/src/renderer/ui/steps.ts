/**
 * The form's two steps as the screen shows them: each step's name and what it holds in words,
 * which may be opened, and the one-line account of where the form stands ("1 of 2 · Choose what
 * to optimize") that the sidebar's draft and the collapsed step list share.
 */
import {
  chosenEntry,
  entryName,
  isDone,
  type NewRunContext,
  reachableSteps,
  type StepId,
  stepIds,
} from '../../state/index.js';
import {entriesIn, projectLabels} from './view.js';

const STEP_TITLES: Readonly<Record<StepId, string>> = {
  pick: 'Choose what to optimize',
  review: 'Review and start',
};

export interface StepView {
  readonly id: StepId;
  readonly title: string;
  /** What the step holds in words, once there is something to say. */
  readonly answer: string | null;
  /** Whether the step may be opened: every step before it is answered. */
  readonly reached: boolean;
}

/** The task picked in words: the example's, or the project's and its task; null before one is. */
export function pickedWords(context: NewRunContext): string | null {
  const {answers} = context;
  if (answers.from === 'example') {
    const entry = chosenEntry(answers, entriesIn(context));
    return entry === null ? null : entryName(entry);
  }
  const path = answers.project?.path;
  if (answers.from !== 'project' || path === undefined || answers.task === null) return null;
  return `${projectLabels([path]).get(path) ?? path} · ${answers.task}`;
}

/** The steps, each with what it holds (`review`: what Review says of itself) and whether it may be opened. */
export function stepViews(context: NewRunContext, review: string | null = null): StepView[] {
  const entries = entriesIn(context);
  const reached = reachableSteps(context.answers, entries);
  return stepIds().map(id => ({
    id,
    title: STEP_TITLES[id],
    answer:
      id === 'review' ? review : isDone(id, context.answers, entries) ? pickedWords(context) : null,
    reached: reached.includes(id),
  }));
}

/** "1 of 2 · Choose what to optimize": where the form stands. */
export function progressLine(views: readonly StepView[], at: StepId): string {
  const index = Math.max(
    0,
    views.findIndex(view => view.id === at),
  );
  return `${index + 1} of ${views.length} · ${views[index]?.title ?? ''}`;
}

/**
 * What the sidebar says of an unfinished form: the task picked (a short name that fits a row), and
 * where it stands.
 */
export function draftWords(
  context: NewRunContext,
): {readonly title: string; readonly detail: string} | null {
  if (context.answers.from === null) return null;
  return {
    title: pickedWords(context) ?? 'New run',
    detail: progressLine(stepViews(context), context.at),
  };
}
