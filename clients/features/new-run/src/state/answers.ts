/**
 * What the guided form has been told, and the steps it asks because of it. `Answers` is a plain
 * data fold: a change to one answer clears exactly the answers that depended on it (the later
 * questions of the same path), never an earlier one or an unrelated one, and a change to the same
 * value is no change. The steps are a pure function of the answers and the catalogue; a step may
 * be reached once every step before it is answered.
 */
import {
  allExamples,
  type CatalogueEntry,
  type Domain,
  domainOptions,
  entriesFor,
  examplesOf,
  hasDetails,
  modelGroups,
  tasksOfExample,
} from './catalogue.js';

/** A project of the user's, on a machine: a recent one, or a folder they opened. */
export interface OwnProject {
  readonly source: 'recent' | 'folder';
  readonly path: string;
}

export type StepId =
  | 'start'
  | 'domain'
  | 'model'
  | 'hardware'
  | 'structure'
  | 'workload'
  | 'example'
  | 'variant'
  | 'project'
  | 'task'
  | 'goal'
  | 'where'
  | 'agent'
  | 'review';

export interface Answers {
  readonly from: 'example' | 'project' | null;
  readonly domain: Domain | null;
  /** The served model's name (LLM serving). */
  readonly model: string | null;
  /** The hardware's key (`hardwareKey`). */
  readonly hardware: string | null;
  /** A data-structures example. */
  readonly structure: string | null;
  /** The task of that example: the workload it drives. */
  readonly workload: string | null;
  /** A databases or microservices example, or any example from a helper with no catalogue. */
  readonly example: string | null;
  /** The task, when several tasks of the chosen example remain. */
  readonly variant: string | null;
  readonly project: OwnProject | null;
  readonly task: string | null;
  /** The goal was read and accepted. */
  readonly goalSeen: boolean;
}

export const NO_ANSWERS: Answers = {
  from: null,
  domain: null,
  model: null,
  hardware: null,
  structure: null,
  workload: null,
  example: null,
  variant: null,
  project: null,
  task: null,
  goalSeen: false,
};

export type AnswerChange =
  | {readonly kind: 'from'; readonly from: 'example' | 'project'}
  | {readonly kind: 'domain'; readonly domain: Domain}
  | {readonly kind: 'model'; readonly model: string}
  | {readonly kind: 'hardware'; readonly hardware: string}
  | {readonly kind: 'structure'; readonly example: string}
  | {readonly kind: 'workload'; readonly task: string}
  | {readonly kind: 'example'; readonly example: string}
  | {readonly kind: 'variant'; readonly task: string}
  | {readonly kind: 'project'; readonly project: OwnProject}
  | {readonly kind: 'task'; readonly task: string}
  | {readonly kind: 'goal'};

type Field = keyof Answers;

/** Every answer that a change of the key answers makes stale: the later questions that follow it. */
const DEPENDENTS: Readonly<Record<Field, readonly Field[]>> = {
  from: [
    'domain',
    'model',
    'hardware',
    'structure',
    'workload',
    'example',
    'variant',
    'project',
    'task',
    'goalSeen',
  ],
  domain: ['model', 'hardware', 'structure', 'workload', 'example', 'variant', 'goalSeen'],
  model: ['hardware', 'variant', 'goalSeen'],
  hardware: ['variant', 'goalSeen'],
  structure: ['workload', 'goalSeen'],
  workload: ['goalSeen'],
  example: ['variant', 'goalSeen'],
  variant: ['goalSeen'],
  project: ['task', 'goalSeen'],
  task: ['goalSeen'],
  goalSeen: [],
};

/** `answers` with `field` set to `value` and everything that depended on it cleared. */
function set<K extends Field>(answers: Answers, field: K, value: Answers[K]): Answers {
  if (answers[field] === value) return answers;
  const cleared = Object.fromEntries(
    DEPENDENTS[field].map(each => [each, NO_ANSWERS[each]] as const),
  );
  return {...answers, ...cleared, [field]: value};
}

function projectsEqual(left: OwnProject | null, right: OwnProject): boolean {
  return left !== null && left.source === right.source && left.path === right.path;
}

/** Which path a question belongs to: the answer to another path's question is no answer. */
function onPath(answers: Answers, change: AnswerChange): boolean {
  switch (change.kind) {
    case 'from':
      return true;
    case 'project':
    case 'task':
      return answers.from === 'project';
    case 'goal':
      return answers.from !== null;
    default:
      return answers.from === 'example';
  }
}

/** The answers after `change`; a change to a question the answers do not ask changes nothing. */
export function answer(answers: Answers, change: AnswerChange): Answers {
  if (!onPath(answers, change)) return answers;
  switch (change.kind) {
    case 'from':
      return set(answers, 'from', change.from);
    case 'domain':
      return set(answers, 'domain', change.domain);
    case 'model':
      return set(answers, 'model', change.model);
    case 'hardware':
      return set(answers, 'hardware', change.hardware);
    case 'structure':
      return set(answers, 'structure', change.example);
    case 'workload':
      return set(answers, 'workload', change.task);
    case 'example':
      return set(answers, 'example', change.example);
    case 'variant':
      return set(answers, 'variant', change.task);
    case 'project':
      return projectsEqual(answers.project, change.project)
        ? answers
        : set(answers, 'project', change.project);
    case 'task':
      return set(answers, 'task', change.task);
    case 'goal':
      return set(answers, 'goalSeen', true);
  }
}

/** The entries `answers` leave open on the example path, before a task is named. */
export function openEntries(
  answers: Answers,
  entries: readonly CatalogueEntry[],
): CatalogueEntry[] {
  if (!hasDetails(entries)) {
    return answers.example === null ? [] : tasksOfExample(entries, answers.example);
  }
  switch (answers.domain) {
    case 'llm-serving':
      return answers.model === null || answers.hardware === null
        ? []
        : entriesFor(entries, answers.model, answers.hardware);
    case 'data-structures':
      return answers.structure === null ? [] : tasksOfExample(entries, answers.structure);
    case 'databases':
    case 'microservices':
      return answers.example === null ? [] : tasksOfExample(entries, answers.example);
    case null:
      return [];
  }
}

/** The question that names the task among `remaining`, if it is not the only one. */
function taskField(answers: Answers): 'workload' | 'variant' {
  return answers.domain === 'data-structures' ? 'workload' : 'variant';
}

/** The entry the example-path answers name, or null while they do not name one. */
export function chosenEntry(
  answers: Answers,
  entries: readonly CatalogueEntry[],
): CatalogueEntry | null {
  if (answers.from !== 'example') return null;
  const open = openEntries(answers, entries);
  const field = hasDetails(entries) ? taskField(answers) : 'variant';
  const named = field === 'workload' ? answers.workload : answers.variant;
  if (field === 'variant' && open.length === 1) return open[0] ?? null;
  return open.find(entry => entry.task === named) ?? null;
}

/** The ids of the steps `answers` ask, in order. */
export function stepIds(answers: Answers, entries: readonly CatalogueEntry[]): StepId[] {
  const path = ((): StepId[] => {
    if (answers.from === 'project') return ['project', 'task'];
    if (answers.from !== 'example') return [];
    const open = openEntries(answers, entries);
    const several: StepId[] = open.length > 1 ? ['variant'] : [];
    if (!hasDetails(entries)) return ['example', ...several];
    switch (answers.domain) {
      case 'llm-serving':
        return ['domain', 'model', 'hardware', ...several];
      case 'data-structures':
        return ['domain', 'structure', 'workload'];
      case 'databases':
      case 'microservices':
        return ['domain', 'example', ...several];
      case null:
        return ['domain'];
    }
  })();
  return ['start', ...path, 'goal', 'where', 'agent', 'review'];
}

/** What a step's answer depends on beyond `Answers`: whether an agent is chosen. */
export interface StepFacts {
  readonly agent: boolean;
}

/** Whether the step's question is answered. Where and Review have no answer of their own to wait for. */
export function isDone(
  step: StepId,
  answers: Answers,
  entries: readonly CatalogueEntry[],
  facts: StepFacts,
): boolean {
  switch (step) {
    case 'start':
      return answers.from !== null;
    case 'domain':
      return answers.domain !== null;
    case 'model':
      return answers.model !== null;
    case 'hardware':
      return answers.hardware !== null;
    case 'structure':
      return answers.structure !== null;
    case 'workload':
      return answers.workload !== null;
    case 'example':
      return answers.example !== null;
    case 'variant':
      return chosenEntry(answers, entries) !== null;
    case 'project':
      return answers.project !== null;
    case 'task':
      return answers.task !== null;
    case 'goal':
      return answers.goalSeen;
    case 'where':
      return true;
    case 'agent':
      return facts.agent;
    case 'review':
      return false;
  }
}

/** The steps that may be shown now: each is reached when every step before it is answered. */
export function reachableSteps(
  answers: Answers,
  entries: readonly CatalogueEntry[],
  facts: StepFacts,
): StepId[] {
  const ids = stepIds(answers, entries);
  const reached: StepId[] = [];
  for (const id of ids) {
    reached.push(id);
    if (!isDone(id, answers, entries, facts)) break;
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

/** The step to show when `at` is no longer one: the first the answers leave open. */
export function stepAfterChange(
  at: StepId,
  answers: Answers,
  entries: readonly CatalogueEntry[],
  facts: StepFacts,
): StepId {
  const reached = reachableSteps(answers, entries, facts);
  return reached.includes(at) ? at : (reached.at(-1) ?? 'start');
}

/** Whether `entries` still offer the answer `field` holds in `answers`, given the ones before it. */
function offered(
  answers: Answers,
  field: ExampleField,
  entries: readonly CatalogueEntry[],
): boolean {
  const known = hasDetails(entries);
  switch (field) {
    case 'domain':
      return known && domainOptions(entries).some(option => option.domain === answers.domain);
    case 'model':
      return modelGroups(entries).some(group => group.models.some(m => m.name === answers.model));
    case 'hardware':
      return (
        answers.model !== null &&
        entriesFor(entries, answers.model, answers.hardware ?? '').length > 0
      );
    case 'structure':
      return examplesOf(entries, 'data-structures').includes(answers.structure ?? '');
    case 'workload':
      return openEntries(answers, entries).some(entry => entry.task === answers.workload);
    case 'example': {
      const options = known
        ? answers.domain === null
          ? []
          : examplesOf(entries, answers.domain)
        : allExamples(entries);
      return options.includes(answers.example ?? '');
    }
    case 'variant':
      return openEntries(answers, entries).some(entry => entry.task === answers.variant);
  }
}

type ExampleField =
  | 'domain'
  | 'model'
  | 'hardware'
  | 'structure'
  | 'workload'
  | 'example'
  | 'variant';

const EXAMPLE_FIELDS: readonly ExampleField[] = [
  'domain',
  'model',
  'hardware',
  'structure',
  'workload',
  'example',
  'variant',
];

/**
 * `answers` without the example-path answers that `entries` no longer offer, and what depended on
 * them: after the machine's catalogue changed. Answers about the user's own project are kept.
 */
export function pruneAnswers(answers: Answers, entries: readonly CatalogueEntry[]): Answers {
  if (answers.from !== 'example') return answers;
  let current = answers;
  for (const field of EXAMPLE_FIELDS) {
    if (current[field] !== null && !offered(current, field, entries)) {
      current = set(current, field, null);
    }
  }
  return current;
}
