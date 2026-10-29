import {describe, expect, test} from 'bun:test';
import type {MeasureTarget} from '@vibesys/ipc';
import {exampleCatalogue} from '@vibesys/run-fixtures';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {type Answers, NO_ANSWERS} from './answers.js';
import {entriesOf} from './catalogue.js';
import {type Draft, draftOf, flowOfDraft, parseDraft, storeDraft} from './draft.js';
import {flowAnswer, startFlow} from './flow.js';
import {DEFAULT_BUDGET, type Form, SAME_MACHINE} from './form.js';

const FORM: Form = {
  machine: 'ssh:host-a',
  project: null,
  task: null,
  agent: {
    provider: 'claude',
    model: 'opus',
    roles: [{role: 'judge', agent: {provider: 'claude', model: 'sonnet'}, effort: 'high'}],
  },
  budget: {rounds: 4, timeLimitS: 3600, results: 'publish_github'},
  measure: {kind: 'same_machine'},
  advanced: '--note "two words"',
};

function draft(random: Seeded): Draft {
  const answers: Answers = random.pick<Answers>([
    {...NO_ANSWERS, from: 'project', project: {source: 'recent', path: '/p/a'}, task: 'mpmc'},
    {...NO_ANSWERS, from: 'example', domain: 'llm-serving', model: 'Llama 3 8B Instruct'},
    {
      ...NO_ANSWERS,
      from: 'example',
      domain: 'data-structures',
      structure: 'data-structures/stack',
      workload: 'spsc',
      goalSeen: true,
    },
  ]);
  return {
    machine: random.pick(['local', 'ssh:host-a']),
    answers,
    at: random.pick(['start', 'goal', 'review'] as const),
    agent: random.pick([null, FORM.agent]),
    budget: random.pick([DEFAULT_BUDGET, FORM.budget]),
    measure: random.pick<MeasureTarget>([
      SAME_MACHINE,
      {kind: 'modal'},
      {kind: 'slurm', cluster: random.pick(['lab-h100', 'cluster m'])},
    ]),
    advanced: random.pick(['', FORM.advanced]),
    destination: random.pick([null, '~/elsewhere']),
  };
}

describe('the draft a window keeps', () => {
  test('a draft is read back exactly as it was stored', () => {
    for (const seed of SEEDS) {
      const kept = draft(new Seeded(seed));
      expect(parseDraft(storeDraft(kept)), `seed ${seed}`).toEqual(kept);
    }
  });

  test('anything that is not exactly a draft is no draft', () => {
    const kept = storeDraft(draft(new Seeded(1)));
    const fields = JSON.parse(kept) as Record<string, unknown>;
    const broken = (change: Record<string, unknown>): string =>
      JSON.stringify({...fields, ...change});
    expect(parseDraft(null)).toBeNull();
    expect(parseDraft('not json')).toBeNull();
    expect(parseDraft('[]')).toBeNull();
    expect(parseDraft(broken({extra: 1}))).toBeNull();
    expect(parseDraft(broken({machine: 'elsewhere'}))).toBeNull();
    expect(parseDraft(broken({at: 'nowhere'}))).toBeNull();
    expect(parseDraft(broken({budget: {rounds: 0, timeLimitS: null, results: 'keep'}}))).toBeNull();
    expect(parseDraft(broken({answers: {...NO_ANSWERS, from: 'sideways'}}))).toBeNull();
    expect(parseDraft(broken({answers: {from: 'example'}}))).toBeNull();
    expect(parseDraft(broken({agent: {provider: 'claude'}}))).toBeNull();
  });

  test('nothing answered is no draft; an answer makes one that restores the same flow', () => {
    const entries = entriesOf(exampleCatalogue);
    expect(draftOf(startFlow(FORM))).toBeNull();
    let flow = startFlow(FORM);
    for (const change of [
      {kind: 'from', from: 'example'},
      {kind: 'domain', domain: 'data-structures'},
      {kind: 'structure', example: 'data-structures/stack'},
      {kind: 'workload', task: 'mpmc'},
    ] as const) {
      flow = flowAnswer(flow, change, true, entries);
    }
    const kept = draftOf(flow);
    expect(kept).not.toBeNull();
    if (kept === null) return;
    const restored = flowOfDraft(parseDraft(storeDraft(kept)) as Draft, entries);
    expect(restored).toEqual(flow);
  });

  test('where an example is copied to is kept with the draft', () => {
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
    const moved = {
      ...flow,
      form: {
        ...flow.form,
        project: {source: 'example', example: 'data-structures/stack', destination: '~/mine'},
      },
    } as const;
    const kept = draftOf(moved) as Draft;
    expect(kept.destination).toBe('~/mine');
    expect(flowOfDraft(kept, entries).form.project).toEqual(moved.form.project);
  });
});
