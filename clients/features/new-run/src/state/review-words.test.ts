import {describe, expect, test} from 'bun:test';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {DEFAULT_BUDGET, type Form, inputsKey, inputsWaitOn} from './form.js';
import {reviewWords, rolesWaitWords} from './review-words.js';

/** A form with each input there or not, the project an example not made yet or a folder. */
function formOf(random: Seeded): Form {
  return {
    machine: 'local',
    project: random.pick([
      null,
      {source: 'example', example: 'data-structures/stack', destination: '~/stack'},
      {source: 'folder', path: '/p'},
    ] as const),
    task: random.pick([null, 'mpmc']),
    agent: random.bool() ? null : {provider: 'claude', model: 'm', roles: []},
    budget: DEFAULT_BUDGET,
    measure: {kind: 'same_machine'},
    advanced: random.pick(['', '--max-in-flight 3', '--max-rounds 3']),
  };
}

describe('what the inputs wait for', () => {
  test('is nothing exactly when the inputs can be checked, and always has words', () => {
    for (const seed of SEEDS) {
      const form = formOf(new Seeded(seed));
      const on = inputsWaitOn(form);
      expect(on === null, `seed ${seed}`).toBe(inputsKey(form) !== null);
      if (on !== null) {
        expect(rolesWaitWords(on).short).toMatch(/^After /);
        expect(rolesWaitWords(on).sentence).toMatch(/^The roles are read from the machine once /);
      }
    }
  });

  test('an example not made yet waits for its project first', () => {
    const form = formOf(new Seeded(1));
    const example = {
      ...form,
      project: {source: 'example', example: 'e', destination: '~/e'},
      task: 'mpmc',
    } as const;
    expect(inputsWaitOn(example)).toBe('project');
    expect(rolesWaitWords('project').short).toBe('After the project is made');
  });
});

describe('the step list on Review', () => {
  test('says what Review holds until it is opened, then how many checks need the user', () => {
    for (const failed of [0, 1, 3]) {
      expect(reviewWords(failed, false)).toBe('Machine, agent, limits');
    }
    expect(reviewWords(0, true)).toBe('Machine, agent, limits');
    expect(reviewWords(1, true)).toBe('1 check needs you');
    expect(reviewWords(3, true)).toBe('3 checks need you');
  });
});
