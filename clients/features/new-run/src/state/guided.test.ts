import {describe, expect, test} from 'bun:test';
import type {ExampleList, ExampleTask} from '@vibesys/contracts/host-helper';
import {exampleCatalogue, exampleCatalogueWithoutDetails} from '@vibesys/run-fixtures';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {
  type AnswerChange,
  type Answers,
  answer,
  chosenEntry,
  isDone,
  NO_ANSWERS,
  nextStep,
  openEntries,
  pruneAnswers,
  reachableSteps,
  type StepId,
  stepIds,
} from './answers.js';
import {
  allExamples,
  type CatalogueEntry,
  domainOptions,
  entriesOf,
  examplesOf,
  hardwareGroups,
  hasDetails,
  modelGroups,
  tasksOfExample,
} from './catalogue.js';
import {type Flow, flowAnswer, flowBack, flowGo, flowNext, flowSettled, startFlow} from './flow.js';
import {DEFAULT_BUDGET, type Form} from './form.js';

const FACTS = {agent: true} as const;

/** A catalogue drawn from the fixtures' shapes at random: which examples exist, and for what. */
function randomCatalogue(random: Seeded): ExampleList {
  const keep = exampleCatalogue.examples.filter(() => random.int(4) > 0);
  const extra: ExampleTask[] = ['a', 'b', 'c'].slice(0, random.int(3) + 1).map(task => ({
    task,
    domain: 'llm-serving',
    hardware: {accelerator: 'NVIDIA H100', count: 2, vendor: 'nvidia', backend: 'cuda'},
    model: {name: 'Llama 3 8B Instruct', maker: 'meta'},
    workload: null,
    objectives: [],
    goal: '# Goal\n',
    goal_truncated: false,
  }));
  return {
    ...exampleCatalogue,
    examples: [
      ...keep,
      {
        name: 'model-serving/generated',
        category: 'model-serving',
        tasks: extra.map(each => each.task),
        external_repo: false,
        task_details: extra,
      },
    ],
  };
}

/** What each step asks for: the options the screen offers it, as the screen computes them. */
function options(
  step: StepId,
  answers: Answers,
  entries: readonly CatalogueEntry[],
): AnswerChange[] {
  switch (step) {
    case 'start':
      return [
        {kind: 'from', from: 'example'},
        {kind: 'from', from: 'project'},
      ];
    case 'domain':
      return domainOptions(entries).map(({domain}) => ({kind: 'domain', domain}));
    case 'model':
      return modelGroups(entries).flatMap(group =>
        group.models.map(model => ({kind: 'model' as const, model: model.name})),
      );
    case 'hardware':
      return hardwareGroups(entries, answers.model ?? '')
        .flatMap(group => group.options)
        .map(option => ({kind: 'hardware', hardware: option.key}));
    case 'structure':
      return examplesOf(entries, 'data-structures').map(example => ({kind: 'structure', example}));
    case 'workload':
      return tasksOfExample(entries, answers.structure ?? '').map(entry => ({
        kind: 'workload',
        task: entry.task,
      }));
    case 'example':
      return (
        hasDetails(entries)
          ? examplesOf(entries, answers.domain ?? 'databases')
          : allExamples(entries)
      ).map(example => ({kind: 'example', example}));
    case 'variant':
      return openEntries(answers, entries).map(entry => ({kind: 'variant', task: entry.task}));
    case 'project':
      return ['/p/a', '/p/b'].map(path => ({kind: 'project', project: {source: 'folder', path}}));
    case 'task':
      return ['mpmc', 'spsc'].map(task => ({kind: 'task', task}));
    case 'goal':
      return [{kind: 'goal'}];
    case 'where':
    case 'agent':
    case 'review':
      return [];
  }
}

const FIELD: Readonly<Record<StepId, keyof Answers | null>> = {
  start: 'from',
  domain: 'domain',
  model: 'model',
  hardware: 'hardware',
  structure: 'structure',
  workload: 'workload',
  example: 'example',
  variant: 'variant',
  project: 'project',
  task: 'task',
  goal: 'goalSeen',
  where: null,
  agent: null,
  review: null,
};

/** Answers the form step by step as a person does: one valid option each, going on after each. */
function walk(
  random: Seeded,
  entries: readonly CatalogueEntry[],
): {answers: Answers; visited: StepId[]} {
  let answers = NO_ANSWERS;
  let at: StepId = 'start';
  const visited: StepId[] = [];
  for (let guard = 0; guard < 20 && at !== 'review'; guard += 1) {
    visited.push(at);
    const choices = options(at, answers, entries);
    if (choices.length > 0) answers = answer(answers, random.pick(choices));
    at = nextStep(at, reachableSteps(answers, entries, FACTS));
  }
  visited.push(at);
  return {answers, visited};
}

/** The answers of `steps` are the same in `next` as in `before`. */
function expectFields(
  steps: readonly StepId[],
  next: Answers,
  before: Answers,
  label: string,
): void {
  for (const step of steps) {
    const field = FIELD[step];
    if (field !== null) expect(next[field], `${label} ${step}`).toEqual(before[field]);
  }
}

/** The answers of `steps` are cleared in `answers`. */
function expectCleared(steps: readonly StepId[], answers: Answers, label: string): void {
  for (const step of steps) {
    const field = FIELD[step];
    if (field !== null) expect(answers[field], `${label} ${step}`).toEqual(NO_ANSWERS[field]);
  }
}

function entriesFrom(random: Seeded): readonly CatalogueEntry[] {
  return entriesOf(random.bool() ? randomCatalogue(random) : exampleCatalogue);
}

describe('the guided form', () => {
  test('every path of answers reaches Review, having asked exactly the steps it lists', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      const entries =
        random.int(8) === 0 ? entriesOf(exampleCatalogueWithoutDetails) : entriesFrom(random);
      const {answers, visited} = walk(random, entries);
      expect(visited.at(-1), `seed ${seed}`).toBe('review');
      expect(visited, `seed ${seed}`).toEqual(stepIds(answers, entries));
      // Every step before Review is answered, so Review is reached and the run is named.
      for (const step of stepIds(answers, entries).slice(0, -1)) {
        expect(isDone(step, answers, entries, FACTS), `seed ${seed} step ${step}`).toBe(true);
      }
      if (answers.from === 'example') {
        expect(chosenEntry(answers, entries), `seed ${seed}`).not.toBeNull();
      }
    }
  });

  test('changing an earlier answer clears exactly the answers that followed it', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      const entries = entriesFrom(random);
      const {answers} = walk(random, entries);
      const ids = stepIds(answers, entries);
      const questions = ids.filter(
        id => FIELD[id] !== null && options(id, answers, entries).length > 0,
      );
      const step = random.pick(questions);
      const next = answer(answers, random.pick(options(step, answers, entries)));
      if (next[FIELD[step] as keyof Answers] === answers[FIELD[step] as keyof Answers]) {
        // The same answer again changes nothing at all.
        expect(next, `seed ${seed}`).toBe(answers);
        continue;
      }
      const at = ids.indexOf(step);
      expectFields(ids.slice(0, at), next, answers, `seed ${seed}`);
      expectCleared(ids.slice(at + 1), next, `seed ${seed}`);
      // The steps up to the changed one are the same questions as before.
      expect(stepIds(next, entries).slice(0, at + 1)).toEqual(ids.slice(0, at + 1));
    }
  });

  test('the steps that may be shown are a prefix of the steps, up to the first one unanswered', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      const entries = entriesFrom(random);
      let answers = NO_ANSWERS;
      for (let turn = 0; turn < 12; turn += 1) {
        const ids = stepIds(answers, entries);
        const reached = reachableSteps(answers, entries, FACTS);
        expect(reached).toEqual(ids.slice(0, reached.length));
        const open = ids.find(id => !isDone(id, answers, entries, FACTS));
        expect(reached.at(-1)).toBe(open ?? ids.at(-1));
        const step = random.pick(reached);
        const choices = options(step, answers, entries);
        if (choices.length > 0) answers = answer(answers, random.pick(choices));
      }
    }
  });

  test('pruning to a catalogue that lacks an answer clears it and what followed, nothing earlier', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      const entries = entriesOf(exampleCatalogue);
      const {answers} = walk(random, entries);
      if (answers.from !== 'example') continue;
      expect(pruneAnswers(answers, entries)).toBe(answers);
      const fewer = entries.filter(() => random.bool());
      const pruned = pruneAnswers(answers, fewer);
      const ids = stepIds(answers, entries).filter(id => id !== 'goal');
      const firstGone = ids.findIndex(id => {
        const field = FIELD[id];
        return field !== null && answers[field] !== pruned[field];
      });
      if (firstGone < 0) continue;
      expectFields(ids.slice(0, firstGone), pruned, answers, `seed ${seed}`);
      expectCleared(ids.slice(firstGone), pruned, `seed ${seed}`);
      // What the pruned answers still say is offered by the smaller catalogue.
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

/** One thing a person does at the step shown: answer it (and perhaps go on), or move. */
function turnOf(random: Seeded, flow: Flow, entries: readonly CatalogueEntry[]): Flow {
  const choices = options(flow.at, flow.answers, entries);
  if (choices.length > 0) return flowAnswer(flow, random.pick(choices), random.bool(), entries);
  return random.bool() ? flowNext(flow, entries) : flowBack(flow, entries);
}

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

describe('the flow of the guided form', () => {
  test('the run names the project and task the answers name, and the step is always one reached', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      const entries = entriesFrom(random);
      let flow = startFlow(FORM);
      for (let turn = 0; turn < 14; turn += 1) {
        flow = flowGo(flow, random.pick(reachableSteps(flow.answers, entries, FACTS)), entries);
        flow = turnOf(random, flow, entries);
        expect(reachableSteps(flow.answers, entries, FACTS), `seed ${seed}`).toContain(flow.at);
        expectNamed(flow, entries, `seed ${seed}`);
      }
    }
  });

  test('answering a question and going on shows the next step; answering it again keeps the flow', () => {
    const entries = entriesOf(exampleCatalogue);
    const flow = flowAnswer(startFlow(FORM), {kind: 'from', from: 'example'}, true, entries);
    expect(flow.at).toBe('domain');
    const again = flowAnswer(flow, {kind: 'from', from: 'example'}, false, entries);
    expect(again).toBe(flow);
  });

  test('a catalogue that no longer offers the chosen example clears it and moves the step back', () => {
    const entries = entriesOf(exampleCatalogue);
    let flow = startFlow(FORM);
    for (const change of [
      {kind: 'from', from: 'example'},
      {kind: 'domain', domain: 'data-structures'},
      {kind: 'structure', example: 'data-structures/stack'},
      {kind: 'workload', task: 'mpmc'},
    ] as const) {
      flow = flowAnswer(flow, change, true, entries);
    }
    expect(flow.at).toBe('goal');
    const settled = flowSettled(flow, []);
    expect(settled.answers.domain).toBeNull();
    expect(settled.form.project).toBeNull();
    // With no catalogue the example path asks only for an example.
    expect(settled.at).toBe('example');
    // Until the catalogue is known nothing is judged against it.
    expect(flowSettled(flow, null)).toBe(flow);
  });
});
