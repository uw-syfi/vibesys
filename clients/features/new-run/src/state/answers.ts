/**
 * What the form has been told, and its two steps: pick what to optimize, then review and start.
 * `Answers` is a plain data fold: a change to one answer clears exactly the answers that depended on
 * it (a project's task when the project changes), never an earlier or unrelated one, and a change
 * to the same value is no change. Which path is chosen (`from`: an example, or the user's own
 * project) does not clear the other path's answers, so switching between the two keeps both. The
 * steps are a pure function of the answers and the catalogue; Review is reached once the pick names
 * a task.
 */
import type {CatalogueEntry} from './catalogue.js';

/** A project of the user's, on a machine: a recent one, or a folder they opened. */
export interface OwnProject {
  readonly source: 'recent' | 'folder';
  readonly path: string;
}

/** One task of an example, as the catalogue lists it. */
export interface ExampleChoice {
  readonly example: string;
  readonly task: string;
}

export type StepId = 'pick' | 'review';

export interface Answers {
  /** Which path the pick is on: an example's task, or a task of the user's own project. */
  readonly from: 'example' | 'project' | null;
  /** The example task picked. */
  readonly entry: ExampleChoice | null;
  readonly project: OwnProject | null;
  /** The task of `project`. */
  readonly task: string | null;
}

export const NO_ANSWERS: Answers = {from: null, entry: null, project: null, task: null};

export type AnswerChange =
  | {readonly kind: 'from'; readonly from: 'example' | 'project'}
  /** Pick an example's task; it puts the pick on the example path. */
  | {readonly kind: 'entry'; readonly example: string; readonly task: string}
  /** Pick a project of the user's; it puts the pick on the project path. */
  | {readonly kind: 'project'; readonly project: OwnProject}
  | {readonly kind: 'task'; readonly task: string};

function sameProject(left: OwnProject | null, right: OwnProject): boolean {
  return left !== null && left.source === right.source && left.path === right.path;
}

/** The answers after `change`; a task named before any project is chosen changes nothing. */
export function answer(answers: Answers, change: AnswerChange): Answers {
  switch (change.kind) {
    case 'from':
      return answers.from === change.from ? answers : {...answers, from: change.from};
    case 'entry': {
      const same =
        answers.from === 'example' &&
        answers.entry?.example === change.example &&
        answers.entry.task === change.task;
      return same
        ? answers
        : {...answers, from: 'example', entry: {example: change.example, task: change.task}};
    }
    case 'project':
      return answers.from === 'project' && sameProject(answers.project, change.project)
        ? answers
        : {
            ...answers,
            from: 'project',
            project: change.project,
            // The task belongs to the project: another project's is no answer.
            task: sameProject(answers.project, change.project) ? answers.task : null,
          };
    case 'task':
      return answers.project === null || answers.task === change.task
        ? answers
        : {...answers, task: change.task};
  }
}

/** The entry the example-path answers name, or null while they do not name one the catalogue lists. */
export function chosenEntry(
  answers: Answers,
  entries: readonly CatalogueEntry[],
): CatalogueEntry | null {
  const {entry} = answers;
  if (answers.from !== 'example' || entry === null) return null;
  return entries.find(each => each.example === entry.example && each.task === entry.task) ?? null;
}

/** The ids of the steps, in order: the same two for every answer. */
export function stepIds(): StepId[] {
  return ['pick', 'review'];
}

/** Whether the step's question is answered: the pick names a task. Review is never "answered". */
export function isDone(
  step: StepId,
  answers: Answers,
  entries: readonly CatalogueEntry[],
): boolean {
  if (step === 'review') return false;
  return answers.from === 'example'
    ? chosenEntry(answers, entries) !== null
    : answers.from === 'project' && answers.project !== null && answers.task !== null;
}

/** The steps that may be shown now: each is reached when every step before it is answered. */
export function reachableSteps(answers: Answers, entries: readonly CatalogueEntry[]): StepId[] {
  const reached: StepId[] = [];
  for (const id of stepIds()) {
    reached.push(id);
    if (!isDone(id, answers, entries)) break;
  }
  return reached;
}

/** The step after `at`, or `at` itself at the last one or when `at` is not a step. */
export function nextStep(at: StepId, ids: readonly StepId[]): StepId {
  const index = ids.indexOf(at);
  return index < 0 ? at : (ids[index + 1] ?? at);
}

/** The step before `at`, or `at` itself at the first one. */
export function previousStep(at: StepId, ids: readonly StepId[]): StepId {
  const index = ids.indexOf(at);
  return index <= 0 ? at : (ids[index - 1] ?? at);
}

/** The step to show when `at` is no longer one: the last the answers reach. */
export function stepAfterChange(
  at: StepId,
  answers: Answers,
  entries: readonly CatalogueEntry[],
): StepId {
  const reached = reachableSteps(answers, entries);
  return reached.includes(at) ? at : (reached.at(-1) ?? 'pick');
}

/**
 * `answers` without an example task `entries` no longer list: after the machine's catalogue
 * changed. Answers about the user's own project are kept.
 */
export function pruneAnswers(answers: Answers, entries: readonly CatalogueEntry[]): Answers {
  const {entry} = answers;
  if (entry === null) return answers;
  const listed = entries.some(each => each.example === entry.example && each.task === entry.task);
  return listed ? answers : {...answers, entry: null};
}
