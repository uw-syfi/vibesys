/**
 * The guided form as one value: its answers, the step shown, and the run's form that the answers
 * name (`Form.project` and `Form.task` are projections of the answers, written only here). Every
 * change to the answers goes through `flowAnswer`, so the form and the step cannot disagree with
 * them.
 */
import type {ExampleList} from '@vibesys/contracts/host-helper';
import type {HostKey} from '@vibesys/hosts';
import type {Offered} from '@vibesys/ipc';
import type {RecentRun} from '@vibesys/runs';
import {
  type AnswerChange,
  type Answers,
  answer,
  chosenEntry,
  NO_ANSWERS,
  nextStep,
  previousStep,
  pruneAnswers,
  reachableSteps,
  type StepFacts,
  type StepId,
  stepAfterChange,
  stepIds,
} from './answers.js';
import {type CatalogueEntry, entriesOf, exampleLabel} from './catalogue.js';
import type {Form, ProjectChoice} from './form.js';

export interface Flow {
  readonly answers: Answers;
  readonly at: StepId;
  readonly form: Form;
}

/** What a machine's helper answered to `examples.list`, for one machine. */
export interface ExamplesFor {
  readonly for: HostKey;
  /** `failed`: the machine could not list them, or did not answer in time. */
  readonly value: Offered<ExampleList> | {readonly kind: 'failed'};
}

/** What the screen knows of `machine`'s examples: not yet, none, failed, or listed. */
export type ExamplesStatus = 'listing' | 'failed' | 'none' | 'listed';

export function examplesStatus(examples: ExamplesFor | null, machine: HostKey): ExamplesStatus {
  if (examples === null || examples.for !== machine) return 'listing';
  if (examples.value.kind === 'failed') return 'failed';
  return (catalogueFor(examples, machine) ?? []).length === 0 ? 'none' : 'listed';
}

/**
 * The catalogue's entries for `machine`: none from a helper that offers no examples, null while
 * its listing is not known.
 */
export function catalogueFor(
  examples: ExamplesFor | null,
  machine: HostKey,
): readonly CatalogueEntry[] | null {
  if (examples === null || examples.for !== machine) return null;
  if (examples.value.kind !== 'offered') return [];
  const known = ENTRIES.get(examples);
  if (known !== undefined) return known;
  const entries = entriesOf(examples.value.value);
  ENTRIES.set(examples, entries);
  return entries;
}

/** The entries of each listing, made once: the listing is a value that does not change. */
const ENTRIES = new WeakMap<ExamplesFor, readonly CatalogueEntry[]>();

/** Where an example's copy goes unless the user says otherwise. */
export function defaultDestination(example: string): string {
  return `~/vibesys-projects/${exampleLabel(example)}`;
}

/** The project and task the run is of, as `answers` name them, keeping a copy already made. */
function target(
  answers: Answers,
  entries: readonly CatalogueEntry[],
  current: ProjectChoice | null,
): {readonly project: ProjectChoice | null; readonly task: string | null} {
  if (answers.from === 'project') return {project: answers.project, task: answers.task};
  const entry = chosenEntry(answers, entries);
  if (entry === null) return {project: null, task: null};
  const kept =
    current !== null && (current.source === 'example' || current.source === 'created')
      ? current
      : null;
  return {
    project:
      kept !== null && kept.example === entry.example
        ? kept
        : {
            source: 'example',
            example: entry.example,
            destination: defaultDestination(entry.example),
          },
    task: entry.task,
  };
}

/** `form` naming the project and task `answers` name. */
export function formFor(form: Form, answers: Answers, entries: readonly CatalogueEntry[]): Form {
  const named = target(answers, entries, form.project);
  return named.project === form.project && named.task === form.task ? form : {...form, ...named};
}

/** What the steps need to know of the form beyond the answers. */
export function factsOf(form: Form): StepFacts {
  return {agent: form.agent !== null};
}

/** The flow for a fresh form: nothing answered, at the first step. */
export function startFlow(form: Form): Flow {
  return {answers: NO_ANSWERS, at: 'start', form};
}

/**
 * The flow after `change`: the answers it makes, the form they name, and the step shown: the next
 * when `advance` (the question was just answered), else the one shown while it is still reached.
 */
export function flowAnswer(
  flow: Flow,
  change: AnswerChange,
  advance: boolean,
  entries: readonly CatalogueEntry[],
): Flow {
  const answers = answer(flow.answers, change);
  const form = formFor(flow.form, answers, entries);
  const facts = factsOf(form);
  const reached = reachableSteps(answers, entries, facts);
  // The step the question was answered at may be gone with the answers it depended on.
  const wanted = advance ? nextStep(flow.at, reached) : flow.at;
  const at = reached.includes(wanted) ? wanted : (reached.at(-1) ?? 'start');
  return answers === flow.answers && form === flow.form && at === flow.at
    ? flow
    : {answers, at, form};
}

/**
 * The flow once something it depends on is known (a machine's catalogue, its agents): answers the
 * catalogue no longer offers are cleared and the step is one the answers reach. A flow on the
 * example path waits for the catalogue (`null`: not known yet) and is left as it is until then.
 */
export function flowSettled(flow: Flow, entries: readonly CatalogueEntry[] | null): Flow {
  if (entries === null && flow.answers.from === 'example') return flow;
  const known = entries ?? [];
  const answers = pruneAnswers(flow.answers, known);
  const form = formFor(flow.form, answers, known);
  // The agent is chosen from what the machine lists, which may arrive after this: it never moves the step.
  const at = stepAfterChange(flow.at, answers, known, {agent: true});
  return answers === flow.answers && form === flow.form && at === flow.at
    ? flow
    : {answers, at, form};
}

/** The flow shown at `step` when it is reached; else unchanged. */
export function flowGo(flow: Flow, step: StepId, entries: readonly CatalogueEntry[]): Flow {
  const reached = reachableSteps(flow.answers, entries, factsOf(flow.form));
  return reached.includes(step) && step !== flow.at ? {...flow, at: step} : flow;
}

/** The flow one step on, when this step is answered. */
export function flowNext(flow: Flow, entries: readonly CatalogueEntry[]): Flow {
  const reached = reachableSteps(flow.answers, entries, factsOf(flow.form));
  const at = nextStep(flow.at, reached);
  return at === flow.at ? flow : {...flow, at};
}

/** The flow one step back. */
export function flowBack(flow: Flow, entries: readonly CatalogueEntry[]): Flow {
  const at = previousStep(flow.at, stepIds(flow.answers, entries));
  return at === flow.at ? flow : {...flow, at};
}

/**
 * The answers that name the run `run` was, for running it again: its project and task, the goal
 * taken as read, so the flow can open on Review.
 */
export function againAnswers(run: RecentRun): Answers {
  return {
    ...NO_ANSWERS,
    from: 'project',
    project: {source: 'recent', path: run.project},
    task: run.task,
    goalSeen: run.task !== null,
  };
}
