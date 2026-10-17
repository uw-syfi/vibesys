import {describe, it} from 'node:test';
import {DISPLAY_IDS, harnessName, modelName, roleName, wordsOfId} from './display-names.js';
import {expect} from './test-support/expect.js';

describe('the catalog the backend registries generate', () => {
  it('names every harness, model and role id with words, not the id', () => {
    for (const harness of DISPLAY_IDS.harnesses) {
      expect(harnessName(harness)).not.toBe(harness);
      expect(harnessName(harness)).toMatch(/^[A-Z]/);
    }
    for (const model of DISPLAY_IDS.models) {
      const name = modelName(model);
      expect(name).not.toBeNull();
      expect(name).not.toBe(model);
      expect(name).toMatch(/^[A-Z]/);
    }
    for (const role of DISPLAY_IDS.roles) {
      expect(roleName(role)).not.toBe(role);
      expect(roleName(role)).toMatch(/^[A-Z][^_]*$/);
    }
  });

  it('reads the examples a person expects', () => {
    expect(harnessName('claude')).toBe('Claude Code');
    expect(harnessName('codex')).toBe('Codex');
    expect(modelName('claude-opus-5-5')).toBe('Opus 5.5');
    expect(roleName('judge')).toBe('Judge');
    expect(roleName('perf-evaluator')).toBe('Perf evaluator');
    expect(roleName('dynamic-implementer')).toBe('Implementer');
  });
});

describe('what a screen shows for an id no registry names', () => {
  it('reads a role or harness as the words of its id, capitalised', () => {
    expect(roleName('my-plugin_critic')).toBe('My plugin critic');
    expect(harnessName('mock')).toBe('Mock');
  });

  it('gives no name for an unknown model', () => {
    expect(modelName('model-nobody-registered')).toBeNull();
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
