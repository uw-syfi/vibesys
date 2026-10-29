import {expect, test} from 'bun:test';
import {runsDashboardMain} from './index.js';

test('the ./main entry loads', () => {
  expect(runsDashboardMain).toBe('runs-dashboard');
});
