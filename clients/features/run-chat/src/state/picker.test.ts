import {describe, expect, test} from 'bun:test';
import type {HarnessOffer} from '@vibesys/ipc';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {catalogueOf, defaultChoice, modelOptions, providerOptions, withProvider} from './picker.js';

const OFFERS: readonly HarnessOffer[] = [
  {
    provider: 'claude',
    model: 'opus',
    default: false,
    display: {providerName: 'Claude Code', modelName: 'Opus'},
  },
  {
    provider: 'claude',
    model: 'sonnet',
    default: true,
    display: {providerName: 'Claude Code', modelName: 'Sonnet'},
  },
  {provider: 'codex', model: 'gpt-x', default: true},
];

describe('picker', () => {
  test('providers once each in catalogue order, models per provider, raw ids when unnamed', () => {
    expect(providerOptions(OFFERS)).toEqual([
      {value: 'claude', label: 'Claude Code'},
      {value: 'codex', label: 'codex'},
    ]);
    expect(modelOptions(OFFERS, 'claude').map(option => option.label)).toEqual(['Opus', 'Sonnet']);
    expect(modelOptions(OFFERS, 'codex')).toEqual([{value: 'gpt-x', label: 'gpt-x'}]);
  });

  test('a new chat starts on the first provider default; none to choose from is none', () => {
    expect(defaultChoice(OFFERS)).toEqual({provider: 'claude', model: 'sonnet'});
    expect(defaultChoice([])).toBeNull();
    expect(catalogueOf({kind: 'unsupported'})).toEqual({kind: 'none'});
    expect(catalogueOf({kind: 'listed', harnesses: []})).toEqual({kind: 'none'});
  });

  test('whichever provider is picked, the pair is one the catalogue offered', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      let choice = defaultChoice(OFFERS);
      for (let step = 0; step < 6; step += 1) {
        choice = withProvider(OFFERS, choice, random.pick(['claude', 'codex']));
        expect(
          OFFERS.some(
            offer => offer.provider === choice?.provider && offer.model === choice?.model,
          ),
        ).toBe(true);
      }
    }
  });
});
