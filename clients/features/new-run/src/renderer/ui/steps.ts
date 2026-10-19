/**
 * The guided form's step list as the screen shows it: each step's name and, once answered, the
 * answer in words; which steps may be opened; and the one-line account of where the form stands
 * ("4 of 8 · Hardware") that the sidebar's draft and the collapsed step list share.
 */
import type {HarnessOffer} from '@vibesys/ipc';
import {
  type AgentPick,
  DOMAIN_WORDS,
  exampleLabel,
  hardwareGroups,
  harnessLabel,
  isDone,
  modelLabel,
  type NewRunContext,
  reachableSteps,
  type StepId,
  stepIds,
  tasksOfExample,
} from '../../state/index.js';
import {entriesIn, machineLabel, projectLabels} from './view.js';

export const STEP_TITLES: Readonly<Record<StepId, string>> = {
  start: 'Start from',
  domain: 'Domain',
  model: 'Model',
  hardware: 'Hardware',
  structure: 'Structure',
  workload: 'Workload',
  example: 'Example',
  variant: 'Task',
  project: 'Project',
  task: 'Task',
  goal: 'Goal',
  where: 'Where it runs',
  agent: 'Agent',
  review: 'Review and start',
};

export interface StepView {
  readonly id: StepId;
  readonly title: string;
  /** The answer in words, once the step is answered. */
  readonly answer: string | null;
  /** Whether the step may be opened: every step before it is answered. */
  readonly reached: boolean;
}

function offersIn(context: NewRunContext): readonly HarnessOffer[] | null {
  return context.harnesses?.for === context.form.machine ? context.harnesses.list : null;
}

/** An agent in words ("Opus 4.5 · Claude Code"), or null before the machine's harnesses are known. */
export function pickWords(context: NewRunContext, pick: AgentPick): string | null {
  const offers = offersIn(context);
  if (offers === null) return null;
  return `${modelLabel(offers, pick.provider, pick.model)} \u00b7 ${harnessLabel(offers, pick.provider)}`;
}

/** The run's agent in words, or null before one is chosen. */
export function agentWords(context: NewRunContext): string | null {
  const {agent} = context.form;
  return agent === null ? null : pickWords(context, agent);
}

/** A step's answer in words, from the form; null while it has none. */
type Words = (context: NewRunContext) => string | null;

function workloadWords(context: NewRunContext): string | null {
  const {structure, workload} = context.answers;
  if (structure === null || workload === null) return null;
  const entry = tasksOfExample(entriesIn(context), structure).find(each => each.task === workload);
  return entry?.details?.workload?.name ?? workload;
}

function hardwareWords(context: NewRunContext): string | null {
  const {hardware, model} = context.answers;
  return (
    hardwareGroups(entriesIn(context), model ?? '')
      .flatMap(group => group.options)
      .find(option => option.key === hardware)?.label ?? hardware
  );
}

function projectWords(context: NewRunContext): string | null {
  const path = context.answers.project?.path;
  return path === undefined ? null : (projectLabels([path]).get(path) ?? path);
}

const WORDS: Readonly<Record<StepId, Words>> = {
  start: ({answers}) => {
    if (answers.from === null) return null;
    return answers.from === 'example' ? 'An example' : 'Your project';
  },
  domain: ({answers}) => (answers.domain === null ? null : DOMAIN_WORDS[answers.domain].name),
  model: ({answers}) => answers.model,
  hardware: hardwareWords,
  structure: ({answers}) => (answers.structure === null ? null : exampleLabel(answers.structure)),
  workload: workloadWords,
  example: ({answers}) => (answers.example === null ? null : exampleLabel(answers.example)),
  variant: ({answers}) => answers.variant,
  project: projectWords,
  task: ({answers}) => answers.task,
  goal: ({answers}) => (answers.goalSeen ? 'Reviewed' : null),
  where: ({form}) => machineLabel(form.machine),
  agent: agentWords,
  review: () => null,
};

/** The steps the answers ask, each with its answer and whether it may be opened. */
export function stepViews(context: NewRunContext): StepView[] {
  const entries = entriesIn(context);
  const reached = reachableSteps(context.answers, entries, {agent: context.form.agent !== null});
  return stepIds(context.answers, entries).map(id => ({
    id,
    title: STEP_TITLES[id],
    answer: isDone(id, context.answers, entries, {agent: context.form.agent !== null})
      ? WORDS[id](context)
      : null,
    reached: reached.includes(id),
  }));
}

/** "4 of 8 · Hardware": where the form stands. */
export function progressLine(views: readonly StepView[], at: StepId): string {
  const index = Math.max(
    0,
    views.findIndex(view => view.id === at),
  );
  return `${index + 1} of ${views.length} · ${views[index]?.title ?? ''}`;
}

/**
 * What the sidebar says of an unfinished form: the latest thing chosen (a short name that fits a
 * row), and where it stands.
 */
export function draftWords(
  context: NewRunContext,
): {readonly title: string; readonly detail: string} | null {
  if (context.answers.from === null) return null;
  const views = stepViews(context);
  const chosen = views
    .slice(
      1,
      views.findIndex(view => view.id === 'goal'),
    )
    .flatMap(view => (view.answer === null ? [] : [view.answer]));
  return {
    title: chosen.at(-1) ?? views[0]?.answer ?? 'New run',
    detail: progressLine(views, context.at),
  };
}
