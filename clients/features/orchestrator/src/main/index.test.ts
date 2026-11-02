import {expect, test} from 'bun:test';
import {orchestratorMain} from './index.js';

test('the ./main entry loads', () => {
  expect(orchestratorMain).toBe('orchestrator');
});
