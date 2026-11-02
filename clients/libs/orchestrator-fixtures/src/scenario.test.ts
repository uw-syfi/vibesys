import {describe, expect, test} from 'bun:test';
import {
  decodeValidated,
  EventSchema,
  encodeMessage,
  Harness,
  PlanSchema,
} from '@vibesys/orchestrator-proto';
import {applyEvent, emptyState, fanoutOverTime, runAgents} from '@vibesys/orchestrator-state';
import fc from 'fast-check';
import {generateScenario} from './index.js';

describe('a generated run', () => {
  test('every event is valid on the wire and the sequence has no gaps', () => {
    fc.assert(
      fc.property(
        fc.integer({min: 1, max: 5000}),
        fc.integer({min: 0, max: 10}),
        fc.boolean(),
        (seed, subagents, live) => {
          const scenario = generateScenario({seed, subagents, live});
          scenario.events.forEach((event, index) => {
            expect(event.sequence).toBe(BigInt(index + 1));
            const decoded = decodeValidated(EventSchema, encodeMessage(EventSchema, event));
            expect(decoded.sequence).toBe(event.sequence);
          });
          for (const plan of scenario.plans)
            decodeValidated(PlanSchema, encodeMessage(PlanSchema, plan));
        },
      ),
      {numRuns: 60},
    );
  });

  test('the same options give the same run', () => {
    const options = {seed: 7, subagents: 9, live: true} as const;
    expect(generateScenario(options)).toEqual(generateScenario(options));
  });

  test('the long fixture has ten agents, both harnesses, and several open at once', () => {
    const scenario = generateScenario({seed: 11, subagents: 9, live: true});
    const state = scenario.events.reduce(applyEvent, emptyState);
    const agents = runAgents(state, scenario.runId);
    expect(agents).toHaveLength(10);
    expect(new Set(agents.map(agent => agent.harness))).toEqual(
      new Set([Harness.CLAUDE_CODE, Harness.CODEX]),
    );
    const peak = Math.max(...fanoutOverTime(state, scenario.runId).map(point => point.live));
    expect(peak).toBeGreaterThan(1);
  });
});
