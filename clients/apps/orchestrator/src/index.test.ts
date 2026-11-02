import {expect, test} from 'bun:test';
import {orchestrator} from './index.js';

test('the . entry loads', () => {
  expect(orchestrator).toBe('orchestrator');
});
