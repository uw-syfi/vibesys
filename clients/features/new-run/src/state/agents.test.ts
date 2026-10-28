import {describe, expect, test} from 'bun:test';
import type {HarnessOffer} from '@vibesys/ipc';

import {agentGroups} from './agents.js';

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
});
