import {describe, expect, test} from 'bun:test';
import {readdirSync, readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import type {RunEvent} from '@vibesys/backend-client';
import {initialCoreState, reduceEventBatch} from './core-state.js';

const EVENT_CORPUS = resolve(import.meta.dir, '../../../tests/conformance/events');

function eventFixtures(): Array<{name: string; event: RunEvent}> {
  return readdirSync(EVENT_CORPUS)
    .filter(name => name.endsWith('.json'))
    .sort()
    .map(name => ({
      name,
      event: JSON.parse(readFileSync(resolve(EVENT_CORPUS, name), 'utf8')) as RunEvent,
    }));
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
});
