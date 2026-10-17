import {describe, it} from 'node:test';
import {CODE_IDS, roleName, wordsOfId} from './display-names.js';
import {expect} from './test-support/expect.js';

describe('the catalog the backend declarations generate', () => {
  it('names every role id with words, not the id', () => {
    for (const role of CODE_IDS.roles) {
      expect(roleName(role)).not.toBe(role);
      expect(roleName(role)).toMatch(/^[A-Z][^_]*$/);
    }
  });

  it('reads the examples a person expects', () => {
    expect(roleName('judge')).toBe('Judge');
    expect(roleName('perf-evaluator')).toBe('Perf evaluator');
    expect(roleName('dynamic-implementer')).toBe('Implementer');
  });

  it('lists the harness and model ids the screens must not draw', () => {
    expect(CODE_IDS.harnesses.length).toBeGreaterThan(0);
    expect(CODE_IDS.models.length).toBeGreaterThan(0);
  });
});

describe('what a screen shows for a role no declaration names', () => {
  it('reads it as the words of its id, capitalised', () => {
    expect(roleName('my-plugin_critic')).toBe('My plugin critic');
  });

  it('leaves text that is already words as it is', () => {
    expect(roleName('Lock-free ring')).toBe('Lock-free ring');
    expect(roleName('Spin then park')).toBe('Spin then park');
  });

  it('turns every kebab, snake or dotted id into capitalised words, once', () => {
    const parts = ['a', 'judge', 'perf', 'x1', 'v2'];
    for (let seed = 0; seed < 200; seed += 1) {
      const count = 1 + (seed % 4);
      const joiner = ['-', '_', '.'][seed % 3] ?? '-';
      const id = Array.from({length: count}, (_, at) => parts[(seed + at * 3) % parts.length]).join(
        joiner,
      );
      const words = wordsOfId(id);
      expect(words.charAt(0)).toBe(words.charAt(0).toUpperCase());
      expect(words).not.toMatch(/[-_]/);
      expect(wordsOfId(words)).toBe(words);
    }
  });
});
