import {expect, test} from 'bun:test';
import {runsDashboardRenderer} from './index.js';

test('the ./renderer entry loads', () => {
  expect(runsDashboardRenderer).toBe('runs-dashboard');
});
