import {describe, expect, it} from 'bun:test';
import {event} from '@vibesys/contracts/testing';
import {initialCoreState, reduceEvent} from './index.js';

function started(sequence: number, execution: string, provider: string, model: string) {
  return event(sequence, 'agent_execution_started', {
    execution_id: execution,
    agent_kind: 'implementer',
    data: {
      kind: 'agent_execution_started',
      stage: 'implement',
      activity: {kind: 'agent_execution_activity_changed', mode: 'thinking', summary: ''},
      provider,
      model,
    },
  });
}

function spent(sequence: number, execution: string | undefined, input: number) {
  return event(sequence, 'turn_usage_recorded', {
    ...(execution === undefined ? {} : {execution_id: execution}),
    data: {kind: 'turn_usage_recorded', input_tokens: input, total_cost_usd: 0.5},
  });
}

describe('usage records of the core', () => {
  it('joins a turn to the harness and model of its execution, and sums to the ledger', () => {
    const state = [
      started(1, 'e1', 'codex', 'gpt-6-luna'),
      spent(2, 'e1', 100),
      started(3, 'e2', 'claude', 'sonnet-5'),
      spent(4, 'e2', 40),
      spent(5, 'e1', 60),
    ].reduce(reduceEvent, initialCoreState());

    expect(state.usageRecords.map(r => [r.harness, r.model, r.role, r.tokens.input])).toEqual([
      ['codex', 'gpt-6-luna', 'implementer', 100],
      ['claude', 'sonnet-5', 'implementer', 40],
      ['codex', 'gpt-6-luna', 'implementer', 60],
    ]);
    const input = state.usageRecords.reduce((sum, r) => sum + (r.tokens.input ?? 0), 0);
    expect(input).toBe(state.tokens.total.input ?? Number.NaN);
  });

  it('keeps a turn whose execution start was never seen, naming nothing it does not know', () => {
    const state = [spent(1, 'ghost', 5), spent(2, undefined, 7)].reduce(
      reduceEvent,
      initialCoreState(),
    );

    expect(state.usageRecords).toHaveLength(2);
    for (const record of state.usageRecords) {
      expect([record.role, record.harness, record.model]).toEqual([null, null, null]);
    }
  });
});
