import {expect, test} from 'bun:test';
import {runsDashboardState} from './index.js';

test('the ./state entry loads', () => {
  expect(runsDashboardState).toBe('runs-dashboard');
});
