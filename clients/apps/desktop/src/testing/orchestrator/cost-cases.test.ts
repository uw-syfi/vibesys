import {describe, expect, it} from '@effect/vitest';
import {decodeValidated, encodeMessage, HostCostSchema} from '@vibesys/orchestrator-proto';
import {spendSpanMs} from '../../state/index.js';
import {applyChange} from '../../state/orchestrator/fold.js';
import {emptyState} from '../../state/orchestrator/state.js';
import {COST_CASES, costCaseScenario, costCaseView} from './cost-cases.js';
import {servedHostCost} from './served-host-cost.js';

const DAY = 86_400_000;

describe('the hosts of the Cost page', () => {
  it('are valid on the wire, and the same every time they are made', () => {
    for (const name of COST_CASES) {
      const scenario = costCaseScenario(name);
      const cost = servedHostCost(scenario.changes, scenario.endMs + 60_000);
      decodeValidated(HostCostSchema, encodeMessage(HostCostSchema, cost));
      expect(costCaseScenario(name)).toEqual(scenario);
    }
  });

  it('keep the log whole: its sequence has no gap, so a host that serves it stays live', () => {
    for (const name of COST_CASES) {
      const {changes} = costCaseScenario(name);
      expect(changes.map(change => Number(change.sequence))).toEqual(
        changes.map((_, at) => at + 1),
      );
    }
  });

  it('are three worlds: a run with no finished turn, one short run, and a week on several models', () => {
    const none = costCaseView('none');
    expect([none.runCount, none.turns, none.total.costUsd]).toEqual([1, 0, null]);
    expect(
      Object.keys(costCaseScenario('none').changes.reduce(applyChange, emptyState).turns),
    ).toEqual([]);

    const short = costCaseView('short');
    expect([short.runCount, short.turns, short.unpricedTurns]).toEqual([1, 9, 4]);
    expect(spendSpanMs(short.series)).toBeLessThan(DAY);

    const week = costCaseView('week');
    expect(week.runCount).toBeGreaterThan(10);
    expect(spendSpanMs(week.series)).toBeGreaterThan(6 * DAY);
    expect(week.models.filter(model => model.model.model !== '').length).toBeGreaterThanOrEqual(3);
    expect(week.unpricedTurns).toBeGreaterThan(0);
    expect(week.runs.some(run => run.maxCostUsd !== null)).toBe(true);
  });
});
