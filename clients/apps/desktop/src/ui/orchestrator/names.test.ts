import {describe, expect, it} from '@effect/vitest';
import fc from 'fast-check';
import {applyChange, emptyState, agentNames as numbered} from '../../state/index.js';
import {generateScenario} from '../../testing/orchestrator/index.js';
import {agentLabel} from '../agents/labels.js';
import {agentNames, nameOf} from './names.js';

describe('what an agent is called on screen', () => {
  it('is the tree and transcript name on every screen, for any run', () => {
    fc.assert(
      fc.property(
        fc.integer({min: 1, max: 40}),
        fc.integer({min: 0, max: 8}),
        (seed, subagents) => {
          const scenario = generateScenario({seed, subagents, live: true});
          const state = scenario.changes.reduce(applyChange, emptyState);
          const names = agentNames(state, scenario.runId);
          const tree = numbered(state, scenario.runId);
          expect(names.size).toBe(tree.size);
          for (const [id, name] of tree) expect(names.get(id)).toBe(agentLabel(name));
          // A name is shared by no two agents, and an id is never shown for one.
          expect(new Set(names.values()).size).toBe(names.size);
          for (const id of tree.keys()) expect(nameOf(names, id)).not.toBe(id);
        },
      ),
      {seed: 61, numRuns: 30},
    );
  });

  it('is never a raw id for an agent the run does not have', () => {
    expect(nameOf(new Map(), 'agent-01H')).toBe('An agent');
  });
});
