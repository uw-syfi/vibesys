import {describe, expect, it} from '@effect/vitest';
import fc from 'fast-check';
import type {CostUsage} from '../../state/index.js';
import {installDom, mount} from '../../testing/dom/harness.js';
import {AgentTypeTable, agentTypeLine} from './agent-types.js';

installDom();

const usage = fc.record({
  inputTokens: fc.option(fc.nat(1e9), {nil: null}),
  cacheReadTokens: fc.option(fc.nat(1e9), {nil: null}),
  cacheCreationTokens: fc.option(fc.nat(1e9), {nil: null}),
  outputTokens: fc.option(fc.nat(1e9), {nil: null}),
  reasoningTokens: fc.option(fc.nat(1e9), {nil: null}),
  costUsd: fc.option(fc.double({min: 0, max: 1e6, noNaN: true}), {nil: null}),
}) satisfies fc.Arbitrary<CostUsage>;

const line = (spend: Parameters<typeof agentTypeLine>[0]['spend']) =>
  agentTypeLine({key: 'k', name: 'Worker', running: 2, total: 5, spend, precision: 2});

describe('a line of agents by type', () => {
  it('says how many are working of how many there have been, whatever else is known', () => {
    expect(line(null).running).toBe('2 of 5');
  });

  it('writes a cost nobody reported as unknown once turns ran, and as a dash only before any turn', () => {
    fc.assert(
      fc.property(usage, fc.nat(500), (each, turns) => {
        const cost = line({turns, unpricedTurns: 0, usage: {...each, costUsd: null}}).cost.text;
        expect(cost).toBe(turns === 0 ? '—' : 'unknown');
      }),
      {seed: 3},
    );
  });

  it('marks a cost with unpriced turns behind it as a floor, with the same words on hover each time', () => {
    fc.assert(
      fc.property(usage, fc.integer({min: 1, max: 500}), (each, unpriced) => {
        const cost = line({
          turns: unpriced,
          unpricedTurns: unpriced,
          usage: {...each, costUsd: 4.2},
        }).cost;
        expect(cost.text).toBe('≥ $4.20');
        expect(cost.hint).toMatch(/no price/);
      }),
      {seed: 4},
    );
  });

  it('splits tokens and never writes a part nobody reported as zero', () => {
    fc.assert(
      fc.property(usage, fc.integer({min: 1, max: 500}), (each, turns) => {
        const {tokens} = line({turns, unpricedTurns: 0, usage: each});
        if (each.outputTokens === null) expect(tokens.output).toBe('unknown');
        if (
          each.inputTokens === null &&
          each.cacheReadTokens === null &&
          each.cacheCreationTokens === null
        )
          expect(tokens.input).toBe('unknown');
      }),
      {seed: 5},
    );
  });
});

describe('the agents-by-type table', () => {
  it('has one set of columns in one order, whatever page draws it', () => {
    const screen = mount(<AgentTypeTable label="By type" rows={[line(null)]} empty="No agents" />);
    const headers = [...screen.container.querySelectorAll('thead th')].map(
      cell => cell.textContent,
    );
    expect(headers).toEqual(['Type', 'Running', 'Turns', 'Input', 'Output', 'Cost']);
    screen.unmount();
  });
});
