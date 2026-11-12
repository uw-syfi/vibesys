import {describe, expect, it} from '@effect/vitest';
import {FakeDaemon, generateScenario} from './index.js';

describe('the fake daemon run-history window', () => {
  it('leaves later turns out without removing their per-agent items', () => {
    const scenario = generateScenario({seed: 5, subagents: 8, live: true, tree: true, depth: 2});
    const daemon = new FakeDaemon(scenario);
    const whole = daemon.history(scenario.runId, 0n);
    daemon.historyTurnLimit = 1;
    const bounded = daemon.history(scenario.runId, 0n);

    expect(bounded.turns).toEqual(whole.turns.slice(0, 1));
    expect(bounded.turnsLeftOut).toBe(true);
    expect(bounded.turnsLeftOutCount).toBe(whole.turns.length - 1);
    const held = new Set(bounded.turns.map(turn => turn.id));
    expect(
      whole.agents.some(agent =>
        daemon.page(agent.id, 2_000, 0).items.some(item => !held.has(item.turnId)),
      ),
    ).toBe(true);
  });
});
