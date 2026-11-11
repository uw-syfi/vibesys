import {describe, expect, it} from '@effect/vitest';
import {AgentRole} from '@vibesys/orchestrator-proto';
import fc from 'fast-check';
import {workingByRole} from './working-by-role.js';

const ROLES = [AgentRole.LEAD, AgentRole.PLANNER, AgentRole.WORKER, AgentRole.JUDGE] as const;

const summaries = fc.dictionary(
  fc.string({minLength: 1, maxLength: 4}),
  fc.array(fc.record({role: fc.constantFrom(...ROLES), total: fc.nat(30), working: fc.nat(30)}), {
    maxLength: 4,
  }),
);

describe('the agents working by type across runs', () => {
  it('adds the host’s own counts of each type, and invents none', () => {
    fc.assert(
      fc.property(summaries, runs => {
        const working = workingByRole(
          Object.fromEntries(Object.entries(runs).map(([id, roles]) => [id, {roles}])),
        );
        for (const role of ROLES) {
          const expected = Object.values(runs)
            .flat()
            .filter(each => each.role === role)
            .reduce((all, each) => all + each.working, 0);
          expect(working.get(role) ?? 0).toBe(expected);
        }
        expect(
          [...working.keys()].every(role => ROLES.includes(role as (typeof ROLES)[number])),
        ).toBe(true);
      }),
      {seed: 21},
    );
  });
});
