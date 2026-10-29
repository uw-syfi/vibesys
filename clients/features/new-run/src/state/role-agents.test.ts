import {describe, expect, test} from 'bun:test';
import type {HarnessOffer} from '@vibesys/ipc';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {roleHarnesses, roleModels, roleProviders} from './agents.js';
import {type AgentChoice, defaultRole, type Form, prefill, runInputs} from './form.js';
import {pickedRoleAgent, roleAgent, rolesWithin} from './roles.js';

type Maker = 'anthropic' | 'openai' | 'google';
const PROVIDERS = ['claude', 'codex', 'opencode'] as const;
const MAKERS: readonly Maker[] = ['anthropic', 'openai', 'google'];

/** An offer list of arbitrary providers, makers, generations and families; a model may be on several providers. */
function arbitraryOffers(random: Seeded): HarnessOffer[] {
  const count = 1 + random.int(10);
  return Array.from({length: count}, (_, index) => {
    const provider = random.pick(PROVIDERS);
    const maker = random.pick(MAKERS);
    const generation = random.pick([null, 1, 2, 3]);
    const family = random.pick([undefined, 'Alpha', 'Beta']);
    return {
      provider,
      model: `model-${random.int(6)}-${index % 3}`,
      default: false,
      display: {
        providerName: provider,
        modelName: `Model ${index}`,
        maker,
        ...(family === undefined ? {} : {family}),
        ...(generation === null
          ? {}
          : {generation: {id: `g${generation}`, name: `Gen ${generation}`, order: generation}}),
      },
    };
  });
}

describe('which harnesses a role may use', () => {
  test('in containers it is the run harness alone; on the host, every harness offered', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      const offers = arbitraryOffers(random);
      const run = random.pick(PROVIDERS);
      expect(roleProviders(offers, run, 'container')).toEqual([run]);
      expect(roleProviders(offers, run, 'host').sort()).toEqual(
        [...new Set(offers.map(offer => offer.provider))].sort(),
      );
    }
  });
});

describe('the models a role may choose among', () => {
  test('each model of an allowed harness is listed once, on no other harness, and groups stay together', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      const offers = arbitraryOffers(random);
      const allowed = PROVIDERS.filter(() => random.bool());
      const prefer = random.pick(PROVIDERS);
      const listed = roleModels(offers, allowed, prefer);
      const expected = new Set(
        offers.filter(offer => allowed.includes(offer.provider as never)).map(offer => offer.model),
      );
      expect(listed.map(each => each.model).sort()).toEqual([...expected].sort());
      expect(new Set(listed.map(each => each.key)).size).toBe(listed.length);
      for (const each of listed) {
        expect(allowed).toContain(each.provider as never);
        // The harness of a model several run is the preferred one when it runs it.
        const runners = roleHarnesses(offers, allowed, each.model);
        expect(runners).toContain(each.provider);
        if (runners.includes(prefer)) expect(each.provider).toBe(prefer);
      }
      const groups = listed.map(each => each.group);
      const firstSeen = [...new Set(groups)];
      expect(groups).toEqual(
        [...groups].sort((a, b) => firstSeen.indexOf(a) - firstSeen.indexOf(b)),
      );
    }
  });

  test('a group names the maker, then the generation, else the family', () => {
    const offers: HarnessOffer[] = [
      {
        provider: 'claude',
        model: 'a',
        default: true,
        display: {
          providerName: 'Claude Code',
          maker: 'anthropic',
          generation: {id: 'g5', name: 'Claude 5', order: 1},
          family: 'Claude',
        },
      },
      {
        provider: 'claude',
        model: 'b',
        default: false,
        display: {providerName: 'Claude Code', maker: 'anthropic', family: 'Claude'},
      },
      {provider: 'codex', model: 'c', default: true, display: {providerName: 'Codex'}},
    ];
    expect(roleModels(offers, ['claude', 'codex'], 'claude').map(each => each.group)).toEqual([
      'Anthropic · Claude 5',
      'Anthropic · Claude',
      'Codex',
    ]);
  });
});

describe('a role that is given the run agent', () => {
  test('stays on following the run; any other agent is kept', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      const run = {provider: random.pick(PROVIDERS), model: `m${random.int(3)}`};
      const picked = {provider: random.pick(PROVIDERS), model: `m${random.int(3)}`};
      const same = picked.provider === run.provider && picked.model === run.model;
      expect(pickedRoleAgent(run, picked)).toEqual(same ? null : picked);
    }
  });

  test('rolesWithin puts back to the run each agent on a harness that is no longer allowed, keeping effort', () => {
    const roles = [
      {role: 'a', agent: {provider: 'claude', model: 'x'}, effort: 'high' as const},
      {role: 'b', agent: {provider: 'codex', model: 'y'}, effort: 'low' as const},
      defaultRole('c'),
    ];
    expect(rolesWithin(roles, ['claude'])).toEqual(
      roles.map(role => (role.role === 'b' ? {...role, agent: null} : role)),
    );
  });
});

describe('the inputs a run starts with', () => {
  const agent = (roles: AgentChoice['roles']): AgentChoice => ({
    provider: 'claude',
    model: 'opus',
    roles,
  });
  const form = (roles: AgentChoice['roles']): Form => ({
    ...prefill('local', []),
    project: {source: 'recent', path: '/p'},
    task: 'mpmc',
    agent: agent(roles),
  });

  test('carry an agent per role that has one, and the effort of the others, never a role model', () => {
    const roles = [
      {role: 'judge', agent: {provider: 'claude', model: 'sonnet'}, effort: 'high' as const},
      {role: 'mutator', agent: null, effort: 'low' as const},
      defaultRole('merger'),
    ];
    const inputs = runInputs(form(roles));
    expect(inputs?.agents).toEqual({judge: {provider: 'claude', model: 'sonnet'}});
    expect(inputs?.options.roles).toEqual([
      {role: 'judge', model: null, reasoning_effort: 'high'},
      {role: 'mutator', model: null, reasoning_effort: 'low'},
    ]);
    expect(roleAgent(agent(roles), roles[1] as (typeof roles)[number])).toEqual({
      provider: 'claude',
      model: 'opus',
    });
  });

  test('with no role changed carry no agents and no role options', () => {
    const inputs = runInputs(form([defaultRole('judge')]));
    expect(inputs?.agents).toEqual({});
    expect(inputs?.options.roles).toEqual([]);
  });
});

test('an inherited run model has one choice, without an alias that normalizes to no change', () => {
  const regression: HarnessOffer[] = [
    {provider: 'claude', model: 'claude-opus-4-5', default: true},
    {provider: 'claude', model: 'claude-sonnet-4-5', default: false},
  ];
  const cases = [regression, ...SEEDS.map(seed => arbitraryOffers(new Seeded(seed)))];
  for (const offers of cases) {
    const all = roleModels(offers, PROVIDERS, 'claude');
    for (const following of all) {
      const choices = roleModels(offers, PROVIDERS, 'claude', following);
      expect(choices).toEqual(all.filter(choice => choice.key !== following.key));
      expect(choices.every(choice => pickedRoleAgent(following, choice) !== null)).toBe(true);
      // Explicit overrides keep the full list, including a value restored from a saved draft.
      expect(roleModels(offers, PROVIDERS, 'claude', null)).toEqual(all);
    }
  }
});
