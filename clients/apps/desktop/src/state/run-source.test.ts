import {describe, expect, it} from 'vitest';
import {generateScenario} from '../testing/orchestrator/index.js';
import {applyChange} from './orchestrator/fold.js';
import {emptyState} from './orchestrator/state.js';
import {isManagedRun} from './run-source.js';

describe('run source capabilities', () => {
  it('grants managed streams from folded managed state, never from a route-shaped id', () => {
    const scenario = generateScenario({seed: 7, subagents: 0, live: true});
    const state = scenario.changes.reduce(applyChange, emptyState);
    expect(isManagedRun(state, scenario.runId)).toBe(true);
    expect(isManagedRun(state, 'generic-only')).toBe(false);
    expect(isManagedRun(state, undefined)).toBe(false);
  });
});
