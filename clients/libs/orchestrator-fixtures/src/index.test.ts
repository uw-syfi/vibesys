import {expect, test} from 'bun:test';
import {orchestratorFixtures} from './index.js';

test('the . entry loads', () => {
  expect(orchestratorFixtures).toBe('orchestrator-fixtures');
});
