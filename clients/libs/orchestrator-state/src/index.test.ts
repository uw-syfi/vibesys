import {expect, test} from 'bun:test';
import {orchestratorState} from './index.js';

test('the . entry loads', () => {
  expect(orchestratorState).toBe('orchestrator-state');
});
