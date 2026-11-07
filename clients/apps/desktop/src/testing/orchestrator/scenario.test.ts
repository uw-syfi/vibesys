import {describe, expect, it} from '@effect/vitest';
import {
  decodeValidated,
  encodeMessage,
  Harness,
  PlanSchema,
  RunChangeSchema,
} from '@vibesys/orchestrator-proto';
import fc from 'fast-check';
import {applyChange} from '../../state/orchestrator/fold.js';
import {emptyState} from '../../state/orchestrator/state.js';
import {runAgents} from '../../state/orchestrator/views.js';
import {generateScenario} from './index.js';

describe('a generated run', () => {
  it('every event is valid on the wire and the sequence has no gaps', () => {
    fc.assert(
      fc.property(
        fc.integer({min: 1, max: 5000}),
        fc.integer({min: 0, max: 10}),
        fc.boolean(),
        (seed, subagents, live) => {
          const scenario = generateScenario({seed, subagents, live});
          scenario.changes.forEach((event, index) => {
            expect(event.sequence).toBe(BigInt(index + 1));
            const decoded = decodeValidated(RunChangeSchema, encodeMessage(RunChangeSchema, event));
            expect(decoded.sequence).toBe(event.sequence);
          });
          for (const plan of scenario.plans)
            decodeValidated(PlanSchema, encodeMessage(PlanSchema, plan));
        },
      ),
      {numRuns: 60},
    );
  });

  it('the same options give the same run', () => {
    const options = {seed: 7, subagents: 9, live: true} as const;
    expect(generateScenario(options)).toEqual(generateScenario(options));
  });

  it('a tree run is valid on the wire, spawns agents under agents, and folds a big group', () => {
    fc.assert(
      fc.property(
        fc.integer({min: 1, max: 500}),
        fc.integer({min: 8, max: 30}),
        fc.boolean(),
        (seed, subagents, live) => {
          const scenario = generateScenario({seed, subagents, live, tree: true});
          scenario.changes.forEach((event, index) => {
            expect(event.sequence).toBe(BigInt(index + 1));
            decodeValidated(RunChangeSchema, encodeMessage(RunChangeSchema, event));
          });
          const state = scenario.changes.reduce(applyChange, emptyState);
          const agents = runAgents(state, scenario.runId);
          expect(agents).toHaveLength(subagents + 1);
          const byId = new Map(agents.map(agent => [agent.id, agent]));
          const depthOf = (id: string): number => {
            const parent = byId.get(id)?.parentAgentId ?? '';
            return parent === '' ? 0 : 1 + depthOf(parent);
          };
          expect(Math.max(...agents.map(agent => depthOf(agent.id)))).toBeGreaterThanOrEqual(3);
          const siblings = new Map<string, number>();
          for (const agent of agents) {
            const key = `${agent.parentAgentId}/${agent.role}`;
            siblings.set(key, (siblings.get(key) ?? 0) + 1);
          }
          expect(Math.max(...siblings.values())).toBeGreaterThanOrEqual(subagents - 8);
        },
      ),
      {numRuns: 25},
    );
  });

  it('the long fixture has ten agents, both harnesses', () => {
    const scenario = generateScenario({seed: 11, subagents: 9, live: true});
    const state = scenario.changes.reduce(applyChange, emptyState);
    const agents = runAgents(state, scenario.runId);
    expect(agents).toHaveLength(10);
    expect(new Set(agents.map(agent => agent.harness))).toEqual(
      new Set([Harness.CLAUDE_CODE, Harness.CODEX]),
    );
  });
});
