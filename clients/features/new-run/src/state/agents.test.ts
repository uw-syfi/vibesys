import {describe, expect, test} from 'bun:test';
import type {HarnessOffer} from '@vibesys/ipc';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {
  agentGroups,
  groupOf,
  modelAtTier,
  offersIn,
  pickGroup,
  pickHarness,
  tierOf,
  tiersIn,
} from './agents.js';

function offer(
  provider: string,
  model: string,
  extra: {family?: string; tier?: number; tierName?: string; default?: boolean} = {},
): HarnessOffer {
  const {family, tier, tierName} = extra;
  return {
    provider,
    model,
    default: extra.default ?? false,
    display: {
      providerName: provider === 'claude' ? 'Claude Code' : provider,
      modelName: model,
      ...(family === undefined ? {} : {family}),
      ...(tier === undefined ? {} : {tier}),
      ...(tierName === undefined ? {} : {tierName}),
    },
  };
}

const OFFERS: readonly HarnessOffer[] = [
  offer('claude', 'haiku', {family: 'Claude', tier: 1, tierName: 'Haiku'}),
  offer('claude', 'sonnet', {family: 'Claude', tier: 2, tierName: 'Sonnet'}),
  offer('claude', 'opus', {family: 'Claude', tier: 3, tierName: 'Opus', default: true}),
  offer('claude', 'opus-old', {family: 'Claude', tier: 3, tierName: 'Opus'}),
  offer('codex', 'gpt-low', {family: 'GPT', tier: 1, tierName: 'Low'}),
  offer('codex', 'gpt-high', {family: 'GPT', tier: 2, tierName: 'High', default: true}),
  offer('opencode', 'sonnet-via-opencode', {family: 'Claude', tier: 2, tierName: 'Sonnet'}),
  offer('opencode', 'local', {}),
];

describe('the agent as the host offers it', () => {
  test('model lines come from the family, a harness without one is its own line', () => {
    expect(agentGroups(OFFERS).map(group => [group.name, group.providers])).toEqual([
      ['Claude', ['claude', 'opencode']],
      ['GPT', ['codex']],
      ['opencode', ['opencode']],
    ]);
  });

  test('a model sits in the line its offer names, and a model not offered is in none', () => {
    expect(groupOf(OFFERS, 'claude', 'opus')?.name).toBe('Claude');
    expect(groupOf(OFFERS, 'opencode', 'local')?.name).toBe('opencode');
    expect(groupOf(OFFERS, 'claude', 'gone')).toBeNull();
  });

  test('tiers run from smallest to strongest, a tier prefers the default, and one tier is no ladder', () => {
    const claude = agentGroups(OFFERS)[0];
    if (claude === undefined) throw new Error('no line');
    const tiers = tiersIn(OFFERS, claude, 'claude');
    expect(tiers.map(tier => tier.name)).toEqual(['Haiku', 'Sonnet', 'Opus']);
    expect(modelAtTier(tiers[2] as (typeof tiers)[number])).toBe('opus');
    expect(tierOf(tiers, 'opus-old')).toBe(2);
    expect(tierOf(tiers, 'local')).toBeNull();
    expect(tiersIn(OFFERS, claude, 'opencode')).toEqual([]);
    expect(offersIn(OFFERS, claude, 'opencode').map(each => each.model)).toEqual([
      'sonnet-via-opencode',
    ]);
  });

  test('choosing a line keeps the harness and the tier where the line has them', () => {
    const groups = agentGroups(OFFERS);
    const claude = groups[0];
    const gpt = groups[1];
    if (claude === undefined || gpt === undefined) throw new Error('no lines');
    expect(pickGroup(OFFERS, gpt, {provider: 'claude', model: 'sonnet'})).toEqual({
      provider: 'codex',
      model: 'gpt-high',
    });
    expect(pickGroup(OFFERS, claude, {provider: 'opencode', model: 'local'})).toEqual({
      provider: 'opencode',
      model: 'sonnet-via-opencode',
    });
    expect(pickHarness(OFFERS, claude, 'opencode', {provider: 'claude', model: 'sonnet'})).toEqual({
      provider: 'opencode',
      model: 'sonnet-via-opencode',
    });
    expect(
      pickHarness(OFFERS, claude, 'claude', {provider: 'opencode', model: 'sonnet-via-opencode'}),
    ).toEqual({provider: 'claude', model: 'sonnet'});
  });

  test('whatever is picked is an offer of the host and belongs to the line picked', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      const groups = agentGroups(OFFERS);
      let current = null as {provider: string; model: string} | null;
      for (let turn = 0; turn < 8; turn += 1) {
        const group = random.pick(groups);
        const provider = random.pick(group.providers);
        const picked = random.bool()
          ? pickGroup(OFFERS, group, current)
          : pickHarness(OFFERS, group, provider, current);
        expect(picked, `seed ${seed}`).not.toBeNull();
        if (picked === null) continue;
        expect(groupOf(OFFERS, picked.provider, picked.model)?.id, `seed ${seed}`).toBe(group.id);
        current = picked;
      }
    }
  });
});
