import {describe, expect, test} from 'bun:test';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {DEFAULT_BUDGET, type Form, inputsKey, inputsWaitOn} from './form.js';
import {rolesWaitWords, signInWords} from './review-words.js';

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

describe('the sign-in check', () => {
  test('names the harness and the machine in its label and each detail', () => {
    expect(signInWords('Claude Code', 'This Mac')).toEqual({
      label: 'Claude Code signed in on This Mac',
      signedIn: 'Signed in on This Mac.',
      signedOut: 'Sign in once from a terminal on This Mac.',
    });
  });
});
