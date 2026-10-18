import {describe, expect, test} from 'bun:test';
import type {HarnessOffer} from '@vibesys/ipc';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {
  agentGroups,
  familiesIn,
  generationsIn,
  groupOf,
  modelAtTier,
  offersIn,
  pickGroup,
  pickHarness,
  scopeOf,
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

interface MadeSpec {
  readonly model: string;
  readonly maker: 'anthropic' | 'openai';
  readonly family: string;
  readonly generation?: {id: string; name: string; order: number};
  readonly tier: number;
  readonly tierName: string;
}

function made(spec: MadeSpec): HarnessOffer {
  const provider = spec.maker === 'anthropic' ? 'claude' : 'codex';
  return {
    provider,
    model: spec.model,
    default: false,
    display: {
      providerName: provider,
      modelName: spec.model,
      family: spec.family,
      tier: spec.tier,
      tierName: spec.tierName,
      maker: spec.maker,
      ...(spec.generation === undefined ? {} : {generation: spec.generation}),
    },
  };
}

const NEW = {id: 'gpt-6', name: 'GPT-6', order: 1};
const OLD = {id: 'gpt-5', name: 'GPT-5', order: 2};
const MADE: readonly HarnessOffer[] = [
  made({
    model: 'gpt-5-sol',
    maker: 'openai',
    family: 'GPT',
    generation: OLD,
    tier: 2,
    tierName: 'Sol',
  }),
  made({
    model: 'gpt-6-luna',
    maker: 'openai',
    family: 'GPT',
    generation: NEW,
    tier: 1,
    tierName: 'Luna',
  }),
  made({
    model: 'gpt-6-sol',
    maker: 'openai',
    family: 'GPT',
    generation: NEW,
    tier: 2,
    tierName: 'Sol',
  }),
  made({model: 'opus', maker: 'anthropic', family: 'Claude', tier: 3, tierName: 'Opus'}),
  made({model: 'haiku', maker: 'anthropic', family: 'Claude', tier: 1, tierName: 'Haiku'}),
];

describe('the agent when the host reports makers and generations', () => {
  test('cards are makers, named as people name them', () => {
    expect(agentGroups(MADE).map(group => [group.id, group.name])).toEqual([
      ['maker:openai', 'OpenAI'],
      ['maker:anthropic', 'Anthropic'],
    ]);
  });

  test('generations are listed newest first, only when there is more than one', () => {
    const [openai, anthropic] = agentGroups(MADE);
    if (openai === undefined || anthropic === undefined) throw new Error('no cards');
    expect(generationsIn(MADE, openai, 'codex', 'GPT').map(each => each.name)).toEqual([
      'GPT-6',
      'GPT-5',
    ]);
    expect(generationsIn(MADE, anthropic, 'claude', 'Claude')).toEqual([]);
  });

  test('the models of a generation are its own, and a tier is only offered within it', () => {
    const [openai] = agentGroups(MADE);
    if (openai === undefined) throw new Error('no cards');
    const six = {family: 'GPT', generation: 'gpt-6'};
    expect(offersIn(MADE, openai, 'codex', six).map(each => each.model)).toEqual([
      'gpt-6-luna',
      'gpt-6-sol',
    ]);
    // GPT-5 has one model: no slider, a list of one.
    expect(tiersIn(MADE, openai, 'codex', {family: 'GPT', generation: 'gpt-5'})).toEqual([]);
    const sol5 = MADE.find(each => each.model === 'gpt-5-sol') as HarnessOffer;
    expect(scopeOf(sol5)).toEqual({family: 'GPT', generation: 'gpt-5'});
    // Moving to the newer generation keeps the tier the reader was on.
    expect(
      pickHarness(MADE, openai, 'codex', {provider: 'codex', model: 'gpt-5-sol'}, six),
    ).toEqual({provider: 'codex', model: 'gpt-6-sol'});
  });

  test('families are offered only when a card has more than one', () => {
    const mixed = [
      ...MADE,
      made({model: 'opus-x', maker: 'anthropic', family: 'Claude Next', tier: 2, tierName: 'X'}),
    ];
    const anthropic = agentGroups(mixed).find(group => group.id === 'maker:anthropic');
    if (anthropic === undefined) throw new Error('no card');
    expect(familiesIn(mixed, anthropic, 'claude')).toEqual(['Claude', 'Claude Next']);
    expect(familiesIn(MADE, anthropic, 'claude')).toEqual([]);
  });
});
