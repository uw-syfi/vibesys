import {expect, test} from 'bun:test';
import {orchestratorRenderer} from './index.js';

test('the ./renderer entry loads', () => {
  expect(orchestratorRenderer).toBe('orchestrator');
});
