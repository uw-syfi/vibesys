import {describe, expect, test} from 'bun:test';
import type {HarnessListing} from '@vibesys/ipc';
import {modelLabel, modelNamesOf} from './model-names.js';

const LISTING: HarnessListing = {
  kind: 'listed',
  harnesses: [
    {
      provider: 'claude',
      model: 'claude-sonnet-5-5',
      default: true,
      display: {providerName: 'Claude Code', modelName: 'Sonnet 5.5'},
    },
    {provider: 'codex', model: 'gpt-6-luna', default: true},
  ],
};

describe('model names', () => {
  test('a model the catalogue names reads as its display name', () => {
    expect(modelLabel(modelNamesOf(LISTING), 'claude-sonnet-5-5')).toBe('Sonnet 5.5');
  });

  test('a model the catalogue does not name, or any model from a host without one, reads as its id', () => {
    expect(modelLabel(modelNamesOf(LISTING), 'gpt-6-luna')).toBe('gpt-6-luna');
    expect(modelLabel(modelNamesOf(LISTING), 'other-model')).toBe('other-model');
    expect(modelLabel(modelNamesOf({kind: 'unsupported'}), 'claude-sonnet-5-5')).toBe(
      'claude-sonnet-5-5',
    );
  });
});
