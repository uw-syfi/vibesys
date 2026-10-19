import {describe, expect, test} from 'bun:test';
import type {UsageRecord} from '@vibesys/contracts/host-helper';
import {Seeded} from '@vibesys/store/testing';
import {type ModelNames, whoBySpan, whoWords} from '../../../state/index.js';
import {whoBadge} from './who-badge.js';

/**
 * What ran a turn, on the badges of the Agents, idea and step faces: the model reads by the name
 * the host's catalogue gives it, and by its id when the catalogue lacks it. Regression: only the
 * header and the Usage face used the names, so these faces still said `claude-sonnet-5-5`.
 */

function record(span: string, model: string | null, harness: string | null): UsageRecord {
  return {
    run_id: 'run-1',
    execution_id: null,
    span_id: span,
    round_label: null,
    role: 'implementer',
    harness,
    model,
    reasoning_effort: null,
    harness_version: null,
    started_at: null,
    ended_at: '2026-10-01T01:00:00Z',
    tokens: {input: 1, cache_read: null, cache_write: null, output: 1, reasoning: null},
    cost_usd: null,
    duration_ms: null,
  };
}

const NAMES: ModelNames = new Map([['claude-sonnet-5-5', 'Sonnet 5.5']]);

describe('who ran a turn', () => {
  test('reads a model by its display name on the badge and in words', () => {
    const who = whoBySpan([record('turn-a', 'claude-sonnet-5-5', 'claude')], NAMES).get('turn-a');
    expect(whoWords(who)).toBe('Sonnet 5.5 on claude');
    expect(whoBadge(who)).toMatchObject({label: 'Sonnet 5.5 on claude', short: 'Sonnet 5.5'});
  });

  test('reads a model the catalogue does not name by its id, and names nothing without a model', () => {
    const records = [record('turn-a', 'other-model', 'claude'), record('turn-b', null, 'claude')];
    const by = whoBySpan(records, NAMES);
    expect(whoWords(by.get('turn-a'))).toBe('other-model on claude');
    expect(whoWords(by.get('turn-b'))).toBe('claude');
    // With no names at all, every model is its id.
    expect(whoWords(whoBySpan(records).get('turn-a'))).toBe('other-model on claude');
  });

  test('never changes the harness, whatever the names (generated)', () => {
    const random = new Seeded(11);
    const models = ['claude-sonnet-5-5', 'gpt-6-luna', 'm-3', 'm-4'];
    for (let round = 0; round < 50; round += 1) {
      const names: ModelNames = new Map(
        models.filter(() => random.bool()).map(model => [model, `Name of ${model}`]),
      );
      const model = random.pick(models);
      const harness = random.pick(['claude', 'codex']);
      const who = whoBySpan([record('t', model, harness)], names).get('t');
      expect(who).toEqual({harness, model: names.get(model) ?? model});
    }
  });
});
