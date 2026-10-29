import {describe, expect, test} from 'bun:test';
import type {HarnessOffer} from '@vibesys/ipc';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {
  catalogueOf,
  defaultChoice,
  modelOptions,
  providerOptions,
  runHarness,
  withProvider,
} from './picker.js';

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

  test('the default is always an offered pair, and the run own pair when it is offered', () => {
    const pairs = OFFERS.map(offer => ({provider: offer.provider, model: offer.model}));
    const strangers = [
      null,
      {provider: 'claude', model: 'unlisted'},
      {provider: 'other', model: 'sonnet'},
    ];
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      const run = random.pick([...pairs, ...strangers]);
      const last = random.pick([...pairs, ...strangers]);
      const choice = defaultChoice(OFFERS, [last, run]);
      expect(
        choice !== null &&
          pairs.some(pair => pair.provider === choice.provider && pair.model === choice.model),
      ).toBe(true);
      // The last thread's pair wins when offered; else the run's; else the first provider's default.
      const expected = [last, run].find(
        pair =>
          pair !== null &&
          pairs.some(each => each.provider === pair.provider && each.model === pair.model),
      );
      expect(choice).toEqual(expected ?? {provider: 'claude', model: 'sonnet'});
    }
  });

  test('the run own pair is its planner, else the first execution that named both', () => {
    const start = (role: string | null, harness: string | null, model: string | null) => ({
      role,
      harness,
      model,
      // These cases name a role and a harness pair. They do not record which executor ran the turn.
      client: null,
      reasoningEffort: null,
      harnessVersion: null,
      startedAt: null,
    });
    expect(runHarness({executionStarts: {}})).toBeNull();
    expect(
      runHarness({
        executionStarts: {
          a: start('implementer', 'codex', 'gpt-x'),
          b: start('planner', 'claude', 'opus'),
        },
      }),
    ).toEqual({provider: 'claude', model: 'opus'});
    expect(
      runHarness({
        executionStarts: {a: start('judge', null, 'm'), b: start('implementer', 'codex', 'gpt-x')},
      }),
    ).toEqual({provider: 'codex', model: 'gpt-x'});
  });
});
