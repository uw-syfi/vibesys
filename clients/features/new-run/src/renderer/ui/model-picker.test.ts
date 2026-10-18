import {expect, test} from 'bun:test';
import type {HarnessOffer} from '@vibesys/ipc';
import {modelAtTier, modelFamilies} from './model-picker.js';

const OFFERS: HarnessOffer[] = [
  {
    provider: 'codex',
    model: 'gpt-low',
    default: false,
    display: {
      providerName: 'Codex',
      family: 'GPT',
      modelName: 'GPT Low',
      tier: 1,
      tierName: 'Low',
    },
  },
  {
    provider: 'codex',
    model: 'gpt-high',
    default: true,
    display: {
      providerName: 'Codex',
      family: 'GPT',
      modelName: 'GPT High',
      tier: 3,
      tierName: 'High',
    },
  },
  {
    provider: 'codex',
    model: 'gpt-high-old',
    default: false,
    display: {
      providerName: 'Codex',
      family: 'GPT',
      modelName: 'GPT High Old',
      tier: 3,
      tierName: 'High',
    },
  },
  {
    provider: 'codex',
    model: 'o-medium',
    default: false,
    display: {
      providerName: 'Codex',
      family: 'o',
      modelName: 'o Medium',
      tier: 2,
      tierName: 'Medium',
    },
  },
  {provider: 'codex', model: 'unranked', default: false},
  {
    provider: 'claude',
    model: 'opus',
    default: true,
    display: {
      providerName: 'Claude Code',
      family: 'Claude',
      modelName: 'Opus',
      tier: 3,
      tierName: 'Opus',
    },
  },
];

test('families and tiers follow host metadata while unranked models stay in the full list', () => {
  const families = modelFamilies(OFFERS, 'codex');
  expect(families.map(family => family.name)).toEqual(['GPT', 'o']);
  expect(families[0]?.tiers.map(tier => [tier.rank, tier.name])).toEqual([
    [1, 'Low'],
    [3, 'High'],
  ]);
  expect(families[1]?.tiers.map(tier => tier.name)).toEqual(['Medium']);
  expect(
    families.flatMap(family => family.tiers.flatMap(tier => tier.offers.map(offer => offer.model))),
  ).not.toContain('unranked');
  const high = families[0]?.tiers[1];
  if (high === undefined) throw new Error('the GPT high tier is missing');
  expect(modelAtTier(high)).toBe('gpt-high');
});
