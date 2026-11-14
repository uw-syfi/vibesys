import {describe, expect, it} from '@effect/vitest';
import {decodeValidated, encodeMessage, HostCostSchema} from '@vibesys/orchestrator-proto';
import fc from 'fast-check';
import {applyChange} from '../../state/orchestrator/fold.js';
import {emptyState} from '../../state/orchestrator/state.js';
import {ms} from '../../state/orchestrator/time.js';
import {fleetScenario, generateScenario} from './index.js';
import {servedHostCost} from './served-host-cost.js';

const NOW = Date.UTC(2026, 10, 7, 12, 0, 0);
const HOUR = 3_600_000;

const fleetOf = (seed: number, runs: number, live: number) =>
  fleetScenario({seed, prefix: 'h0', runs, live, nowMs: NOW});

const cents = (usd: number | undefined): number => Math.round((usd ?? 0) * 100);

describe('the host cost a fleet serves', () => {
  it('is valid on the wire for any fleet', () => {
    fc.assert(
      fc.property(
        fc.integer({min: 1, max: 5000}),
        fc.integer({min: 0, max: 40}),
        fc.integer({min: 0, max: 40}),
        (seed, runs, live) => {
          const cost = servedHostCost(fleetOf(seed, runs, live).changes, NOW);
          decodeValidated(HostCostSchema, encodeMessage(HostCostSchema, cost));
        },
      ),
      {numRuns: 60},
    );
  });

  it('totals the cost of the runs the fleet lists, run by run', () => {
    fc.assert(
      fc.property(
        fc.integer({min: 1, max: 5000}),
        fc.integer({min: 0, max: 40}),
        fc.integer({min: 0, max: 40}),
        (seed, runs, live) => {
          const scenario = fleetOf(seed, runs, live);
          const state = scenario.changes.reduce(applyChange, emptyState);
          const cost = servedHostCost(scenario.changes, NOW);
          const listed = Object.values(state.runs);
          expect(cost.runCount).toBe(listed.length);
          expect(cents(cost.total?.costUsd)).toBe(
            listed.reduce((sum, run) => sum + cents(run.usage?.costUsd), 0),
          );
          for (const line of cost.runs)
            expect(cents(line.usage?.costUsd)).toBe(cents(state.runs[line.runId]?.usage?.costUsd));
          // Every part of the view splits the same turns, so each sums to the total.
          expect(cost.roles.reduce((sum, role) => sum + role.turns, 0)).toBe(cost.turns);
          expect(cost.models.reduce((sum, model) => sum + model.turns, 0)).toBe(cost.turns);
          expect(cost.roles.reduce((sum, role) => sum + cents(role.usage?.costUsd), 0)).toBe(
            cents(cost.total?.costUsd),
          );
          expect(cost.models.reduce((sum, model) => sum + cents(model.usage?.costUsd), 0)).toBe(
            cents(cost.total?.costUsd),
          );
          expect(cost.series?.buckets.reduce((sum, each) => sum + cents(each.costUsd), 0)).toBe(
            cents(cost.total?.costUsd),
          );
        },
      ),
      {numRuns: 60},
    );
  });

  it('counts the last hour from the turns that ended in it', () => {
    const scenario = fleetOf(11, 8, 2);
    const state = scenario.changes.reduce(applyChange, emptyState);
    const cost = servedHostCost(scenario.changes, NOW);
    const asOf = ms(cost.asOf) ?? 0;
    const inHour = Object.values(state.turns).filter(turn => {
      const end = ms(turn.endedAt) ?? 0;
      return end >= asOf - HOUR && end <= asOf;
    });
    expect(cents(cost.lastHourUsd)).toBe(
      inHour.reduce((sum, turn) => sum + cents(turn.usage?.costUsd), 0),
    );
    // A host with live runs is spending now.
    expect(cents(cost.lastHourUsd)).toBeGreaterThan(0);
  });

  it('serves an exact since window without turns or ended runs from before it', () => {
    const scenario = fleetOf(11, 12, 2);
    const state = scenario.changes.reduce(applyChange, emptyState);
    const since = NOW - 24 * HOUR;
    const cost = servedHostCost(scenario.changes, NOW, since);
    const countedRuns = Object.values(state.runs).filter(run => {
      const ended = ms(run.endedAt);
      return ended === null || ended >= since;
    });
    const countedRunIds = new Set(countedRuns.map(run => run.id));
    const countedTurns = Object.values(state.turns).filter(turn => {
      const agent = state.agents[turn.agentId];
      const ended = ms(turn.endedAt);
      return (
        agent !== undefined && countedRunIds.has(agent.runId) && ended !== null && ended >= since
      );
    });

    expect(cost.runCount).toBe(countedRuns.length);
    expect(cost.turns).toBe(countedTurns.length);
    expect(ms(cost.series?.start)).toBe(since);
    expect(cents(cost.total?.costUsd)).toBe(
      countedTurns.reduce((sum, turn) => sum + cents(turn.usage?.costUsd), 0),
    );
    decodeValidated(HostCostSchema, encodeMessage(HostCostSchema, cost));
  });

  it('has unpriced turns somewhere in a fleet, and none that carry a dollar', () => {
    const cost = servedHostCost(fleetOf(11, 8, 2).changes, NOW);
    expect(cost.unpricedTurns).toBeGreaterThan(0);
    expect(cost.unpricedTurns).toBeLessThan(cost.turns);
  });

  it('serves a host with no runs as an empty view with no price', () => {
    const cost = servedHostCost(fleetOf(11, 0, 0).changes, NOW);
    expect(cost.runCount).toBe(0);
    expect(cost.turns).toBe(0);
    expect(cost.runs).toHaveLength(0);
    expect(cost.total?.costUsd).toBeUndefined();
    expect(cost.lastHourUsd).toBeUndefined();
    decodeValidated(HostCostSchema, encodeMessage(HostCostSchema, cost));
  });

  it('totals the finished turns of a single scenario run', () => {
    const scenario = generateScenario({seed: 5, subagents: 4, live: false, tree: true});
    const state = scenario.changes.reduce(applyChange, emptyState);
    const cost = servedHostCost(scenario.changes, scenario.endMs + 60_000);
    const turns = Object.values(state.turns);
    expect(cost.turns).toBe(turns.length);
    expect(cents(cost.total?.costUsd)).toBe(
      turns.reduce((sum, turn) => sum + cents(turn.usage?.costUsd), 0),
    );
    decodeValidated(HostCostSchema, encodeMessage(HostCostSchema, cost));
  });
});
