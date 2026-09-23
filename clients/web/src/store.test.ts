import {expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import type {RunEvent} from '@vibesys/backend-client';
import {createCoreStateStore} from './store.js';

test('folds the recorded run through the shared core-state store', () => {
  const fixture = resolve(import.meta.dir, '../../tui/dev/fixtures/framework-events.jsonl');
  const events = readFileSync(fixture, 'utf8')
    .trim()
    .split('\n')
    .map(line => JSON.parse(line) as RunEvent);
  const store = createCoreStateStore();
  let notifications = 0;
  store.subscribe(() => notifications++);
  store.append(events);
  expect(store.getState().roundLabel).toBe('round-2');
  expect(store.getState().transcript.some(entry => entry.content === 'PASS')).toBe(true);
  expect(notifications).toBe(1);
});
