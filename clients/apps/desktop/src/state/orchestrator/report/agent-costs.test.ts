import {describe, expect, it} from '@effect/vitest';
import {AgentRole} from '@vibesys/orchestrator-proto';
import fc from 'fast-check';
import {PRICED_AND_UNPRICED} from '../../../testing/orchestrator/cost-views.js';
import type {CostView} from '../cost.js';
import {agentCostRows} from './agent-costs.js';
import type {AgentsStrip} from './agents-strip.js';

const ROLES = [AgentRole.LEAD, AgentRole.PLANNER, AgentRole.WORKER, AgentRole.JUDGE] as const;

const strip = fc.uniqueArray(fc.constantFrom(...ROLES), {minLength: 1}).chain(roles =>
  fc.tuple(...roles.map(() => fc.tuple(fc.integer({min: 1, max: 40}), fc.nat(40)))).map(counts => {
    const types = roles.map((role, at) => {
      const [total, working] = counts[at] ?? [1, 0];
      return {role, total, running: Math.min(total, working)};
    });
    return {
      types,
      total: types.reduce((all, type) => all + type.total, 0),
      running: types.reduce((all, type) => all + type.running, 0),
    } satisfies AgentsStrip;
  }),
);

describe('the rows of agents by type', () => {
  it('count the agents as the host’s summary does, whatever the cost view says of how many there were', () => {
    fc.assert(
      fc.property(strip, fc.nat(100), (agents, claimed) => {
        const cost: CostView = {
          ...PRICED_AND_UNPRICED,
          roles: PRICED_AND_UNPRICED.roles.map(role => ({...role, agents: claimed})),
        };
        for (const view of [cost, undefined]) {
          const rows = agentCostRows(agents, view);
          expect(rows.reduce((all, row) => all + row.total, 0)).toBe(agents.total);
          expect(rows.reduce((all, row) => all + row.running, 0)).toBe(agents.running);
        }
      }),
      {seed: 11},
    );
  });

  it('count a type by the cost view until the host has served a summary', () => {
    const rows = agentCostRows({types: [], total: 0, running: 0}, PRICED_AND_UNPRICED);
    expect(rows.map(row => row.total)).toEqual(PRICED_AND_UNPRICED.roles.map(role => role.agents));
    expect(rows.every(row => row.running === 0)).toBe(true);
  });
});
