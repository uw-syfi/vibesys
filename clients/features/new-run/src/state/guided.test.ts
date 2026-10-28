import {describe, expect, test} from 'bun:test';
import {exampleCatalogue, exampleCatalogueWithoutDetails} from '@vibesys/run-fixtures';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {
  type AnswerChange,
  type Answers,
  answer,
  chosenEntry,
  isDone,
  NO_ANSWERS,
  pruneAnswers,
  reachableSteps,
  stepIds,
} from './answers.js';
import {type CatalogueEntry, entriesOf} from './catalogue.js';
import {type Flow, flowAnswer, flowBack, flowGo, flowNext, flowSettled, startFlow} from './flow.js';
import {DEFAULT_BUDGET, type Form} from './form.js';

function entriesFrom(random: Seeded): readonly CatalogueEntry[] {
  const all = entriesOf(random.int(6) === 0 ? exampleCatalogueWithoutDetails : exampleCatalogue);
  return all.filter(() => random.int(4) > 0);
}

/** One change a person can make on the pick page, or one naming a task no catalogue lists. */
function changeOf(random: Seeded, entries: readonly CatalogueEntry[]): AnswerChange {
  const listed = entries.length === 0 ? null : random.pick(entries);
  switch (random.int(5)) {
    case 0:
      return {kind: 'from', from: random.pick(['example', 'project'] as const)};
    case 1:
      return listed === null
        ? {kind: 'entry', example: 'gone/example', task: 'default'}
        : {kind: 'entry', example: listed.example, task: listed.task};
    case 2:
      return {kind: 'entry', example: 'gone/example', task: random.pick(['default', 'mpmc'])};
    case 3:
      return {
        kind: 'project',
        project: {
          source: random.pick(['recent', 'folder'] as const),
          path: random.pick(['/a', '/b']),
        },
      };
    default:
      return {kind: 'task', task: random.pick(['mpmc', 'spsc'])};
  }
}

/** Whether the pick names a task the run can be of. */
function picked(answers: Answers, entries: readonly CatalogueEntry[]): boolean {
  if (answers.from === 'example') return chosenEntry(answers, entries) !== null;
  return answers.from === 'project' && answers.project !== null && answers.task !== null;
}

/** `next` is `answers` with `change` made, and with only what depended on it cleared. */
function expectChanged(answers: Answers, change: AnswerChange, next: Answers): void {
  switch (change.kind) {
    case 'from':
      // Switching sides keeps both sides' answers.
      expect(next).toEqual({...answers, from: change.from});
      return;
    case 'entry':
      expect(next).toEqual({
        ...answers,
        from: 'example',
        entry: {example: change.example, task: change.task},
      });
      return;
    case 'project': {
      const same =
        answers.project?.source === change.project.source &&
        answers.project.path === change.project.path;
      expect(next.entry).toEqual(answers.entry);
      expect(next.from).toBe('project');
      expect(next.task).toBe(same ? answers.task : null);
      return;
    }
    case 'task':
      expect(next).toEqual(answers.project === null ? answers : {...answers, task: change.task});
      return;
  }
}

describe('the answers of the two-step form', () => {
  test('Review is reached exactly when the pick names a listed example task or a project task', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      const entries = entriesFrom(random);
      let answers = NO_ANSWERS;
      for (let turn = 0; turn < 16; turn += 1) {
        answers = answer(answers, changeOf(random, entries));
        const reached = reachableSteps(answers, entries);
        expect(reached, `seed ${seed}`).toEqual(
          picked(answers, entries) ? ['pick', 'review'] : ['pick'],
        );
        expect(isDone('pick', answers, entries)).toBe(picked(answers, entries));
        expect(isDone('review', answers, entries)).toBe(false);
        expect(stepIds()).toEqual(['pick', 'review']);
      }
    }
  });

  test('a change clears only what depended on it: the task when the project changes', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      const entries = entriesFrom(random);
      let answers = NO_ANSWERS;
      for (let turn = 0; turn < 16; turn += 1) {
        const change = changeOf(random, entries);
        const next = answer(answers, change);
        expectChanged(answers, change, next);
        // The same change again is no change at all.
        expect(answer(next, change)).toBe(next);
        answers = next;
      }
    }
  });

  test('pruning drops an example task the catalogue no longer lists, and nothing else', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      const entries = entriesOf(exampleCatalogue);
      let answers = NO_ANSWERS;
      for (let turn = 0; turn < 6; turn += 1) answers = answer(answers, changeOf(random, entries));
      const fewer = entries.filter(() => random.bool());
      const pruned = pruneAnswers(answers, fewer);
      const {entry} = answers;
      const listed =
        entry !== null &&
        fewer.some(each => each.example === entry.example && each.task === entry.task);
      expect(pruned).toEqual({...answers, entry: listed ? entry : null});
      expect(pruneAnswers(pruned, fewer)).toBe(pruned);
    }
  });
});

const FORM: Form = {
  machine: 'local',
  project: null,
  task: null,
  agent: {provider: 'claude', model: 'm', roles: []},
  budget: DEFAULT_BUDGET,
  measure: {kind: 'same_machine'},
  advanced: '',
};

/** The form's project and task are the ones the answers name. */
function expectNamed(flow: Flow, entries: readonly CatalogueEntry[], label: string): void {
  const {answers, form} = flow;
  if (answers.from === 'example') {
    const entry = chosenEntry(answers, entries);
    expect(form.task, label).toBe(entry?.task ?? null);
    expect(form.project?.source ?? null, label).toBe(entry === null ? null : 'example');
  } else if (answers.from === 'project') {
    expect(form.project, label).toEqual(answers.project);
    expect(form.task, label).toBe(answers.task);
  } else {
    expect(form.project, label).toBeNull();
  }
}

describe('the flow of the two-step form', () => {
  test('the run names the project and task the answers name, and the step is always one reached', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      const entries = entriesFrom(random);
      let flow = startFlow(FORM);
      for (let turn = 0; turn < 14; turn += 1) {
        flow = flowGo(flow, random.pick(reachableSteps(flow.answers, entries)), entries);
        switch (random.int(3)) {
          case 0:
            flow = flowAnswer(flow, changeOf(random, entries), random.bool(), entries);
            break;
          case 1:
            flow = flowNext(flow, entries);
            break;
          default:
            flow = flowBack(flow);
        }
        expect(reachableSteps(flow.answers, entries), `seed ${seed}`).toContain(flow.at);
        expectNamed(flow, entries, `seed ${seed}`);
      }
    }
  });

  test('picking an example task and going on shows Review; picking it again keeps the flow', () => {
    const entries = entriesOf(exampleCatalogue);
    const change = {kind: 'entry', example: 'data-structures/stack', task: 'mpmc'} as const;
    const flow = flowAnswer(startFlow(FORM), change, true, entries);
    expect(flow.at).toBe('review');
    expect(flow.form.task).toBe('mpmc');
    expect(flowAnswer(flow, change, false, entries)).toBe(flow);
    expect(flowBack(flow).at).toBe('pick');
  });

  test('a catalogue that no longer lists the picked task clears it and goes back to the pick', () => {
    const entries = entriesOf(exampleCatalogue);
    const flow = flowAnswer(
      startFlow(FORM),
      {kind: 'entry', example: 'data-structures/stack', task: 'mpmc'},
      true,
      entries,
    );
    const settled = flowSettled(flow, []);
    expect(settled.answers.entry).toBeNull();
    expect(settled.form.project).toBeNull();
    expect(settled.at).toBe('pick');
    // Until the catalogue is known nothing is judged against it.
    expect(flowSettled(flow, null)).toBe(flow);
  });
});
