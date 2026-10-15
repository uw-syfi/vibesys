import {describe, expect, test} from 'bun:test';
import {readdirSync, readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import {type RunEvent, validateRunEvent} from '@vibesys/contracts';
import {initialCoreState, reduceEventBatch} from './index.js';

const EVENT_CORPUS = resolve(import.meta.dir, '../../../../tests/conformance/events');

function eventFixtures(): Array<{name: string; event: RunEvent}> {
  return readdirSync(EVENT_CORPUS)
    .filter(name => name.endsWith('.json'))
    .sort()
    .map(name => ({
      name,
      event: JSON.parse(readFileSync(resolve(EVENT_CORPUS, name), 'utf8')) as RunEvent,
    }));
}

function fixtureNamed(name: string): RunEvent {
  const found = eventFixtures().find(fixture => fixture.name === name);
  if (found === undefined) throw new Error(`Missing conformance fixture: ${name}`);
  return found.event;
}

describe('shared conformance event corpus', () => {
  for (const fixture of eventFixtures()) {
    test(`${fixture.name} folds through core state`, () => {
      expect(() => reduceEventBatch(initialCoreState(), [fixture.event])).not.toThrow();
    });
  }

  test('batching does not change the final fold', () => {
    const events = eventFixtures().map(({event}, index) => ({...event, sequence: index + 1}));
    const batched = reduceEventBatch(initialCoreState(), events);
    const incremental = events.reduce(
      (state, event) => reduceEventBatch(state, [event]),
      initialCoreState(),
    );

    expect(incremental).toEqual(batched);
  });

  // The two fixtures above prove every corpus event folds without throwing, but that is also true
  // of an event with no payload at all. These two carry a real, non-empty `data` object (the shape
  // a backend producer actually emits) so the round trip from Python-shaped wire JSON, through the
  // generated schema `validateRunEvent` enforces, to the folded workstream record is proven with
  // real values, not just "did not throw".
  test('turn_usage_recorded.json validates and folds its settled token and cost accounting', () => {
    const validated = validateRunEvent(fixtureNamed('turn_usage_recorded.json'));
    const state = reduceEventBatch(initialCoreState(), [validated]);
    const [record] = Object.values(state.workstreams);
    expect(record?.usage).toEqual({
      inputTokens: 1200,
      cacheCreationInputTokens: 300,
      cacheReadInputTokens: 450,
      outputTokens: 180,
      totalCostUsd: 0.0421,
    });
  });

  test('workstream_finished.json validates and folds its terminal failure detail', () => {
    const validated = validateRunEvent(fixtureNamed('workstream_finished.json'));
    const state = reduceEventBatch(initialCoreState(), [validated]);
    const [record] = Object.values(state.workstreams);
    expect(record?.finished).toBe(true);
    expect(record?.repeatedError).toBe(true);
    expect(record?.lastError).toEqual({
      kind: 'turn-limit',
      role: 'implementer',
      turns: 12,
      limit: 10,
      text: 'exceeded the per-workstream turn budget',
    });
  });
});
