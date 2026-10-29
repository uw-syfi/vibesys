import {describe, expect, test} from 'bun:test';
import type {Span} from '@vibesys/contracts';
import {addTokens, NO_TOKENS, totalTokens} from '@vibesys/run-state';
import {tokensByAgent, turnTokens} from './token-usage.js';
import {ledgerOf} from './usage-fixtures.js';

function turn(id: string, agent: string | null, kind = 'agent_turn'): Span {
  return {
    id,
    parent: null,
    type: 'operation',
    name: 'agent.turn',
    status: 'done',
    started_at: 0,
    ended_at: 1,
    label: {kind, title: id, attrs_json: JSON.stringify(agent === null ? {} : {agent})},
  } as Span;
}

const SPANS = new Map(
  [
    turn('t1', 'impl'),
    turn('t2', 'impl'),
    turn('t3', 'judge'),
    turn('t4', null),
    turn('g1', 'impl', 'gate'),
  ].map(span => [span.id, span]),
);

describe('tokens by agent', () => {
  test('sums each agent’s turns, so the agents together equal the turns the trace names', () => {
    const readings = [
      {span: 't1', input: 100, output: 10},
      {span: 't2', input: 50, cached: 20, output: 5, reasoning: 2},
      {span: 't3', input: 7},
      {span: 't4', input: 1_000},
      {span: 'g1', input: 3},
      {span: 'unknown', input: 9},
    ];
    const ledger = ledgerOf(readings);
    const byAgent = tokensByAgent(SPANS, ledger);
    expect([...byAgent.keys()].sort()).toEqual(['impl', 'judge']);
    expect(byAgent.get('impl')).toEqual({
      input: 150,
      cacheRead: 20,
      cacheWrite: null,
      output: 15,
      reasoning: 2,
    });
    expect(byAgent.get('judge')).toEqual({...NO_TOKENS, input: 7});
    const named = [...byAgent.values()].reduce(addTokens, NO_TOKENS);
    expect(totalTokens(named)).toBe(172);
    // The run's total also holds the turns the trace does not name as an agent's.
    expect(totalTokens(ledger.total)).toBe(172 + 1_000 + 3 + 9);
  });

  test('an agent whose turns reported nothing has no entry; a turn reads null until it reports', () => {
    const ledger = ledgerOf([{span: 't3'}, {span: 't1', input: 5}]);
    expect(tokensByAgent(SPANS, ledger).get('judge')).toEqual(NO_TOKENS);
    expect(tokensByAgent(SPANS, ledgerOf([])).size).toBe(0);
    expect(turnTokens(ledger, 't2')).toBeNull();
    expect(turnTokens(ledger, 't1')).toEqual({...NO_TOKENS, input: 5});
  });
});
