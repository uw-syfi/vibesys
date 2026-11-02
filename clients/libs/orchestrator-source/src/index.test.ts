import {expect, test} from 'bun:test';
import {orchestratorSource} from './index.js';

test('the . entry loads', () => {
  expect(orchestratorSource).toBe('orchestrator-source');
});
