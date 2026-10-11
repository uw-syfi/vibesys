import {expect, test} from 'bun:test';
import {event, eventBatch} from '@vibesys/contracts/testing';
import {createRunStore} from './index.js';

test('distinguishes an omitted active-execution checkpoint from an empty one', () => {
  const store = createRunStore();

  store.dispatch({
    type: 'batch',
    message: eventBatch([
      event(1, 'agent_execution_started', {
        execution_id: 'exec-1',
        agent_kind: 'implementer',
        round_label: 'round-1-implementer',
        data: {
          kind: 'agent_execution_started',
          stage: 'implementation',
          attempt: 1,
          system_prompt: '',
          user_prompt: 'Implement the change',
          activity: {
            kind: 'agent_execution_activity_changed',
            mode: 'thinking',
            summary: 'Starting',
            tool: null,
          },
        },
      }),
    ]),
    reconciliation: {kind: 'extend', historyFloor: 0},
  });

  expect(Object.keys(store.getState().activeExecutions)).toEqual(['exec-1']);

  store.dispatch({
    type: 'batch',
    message: eventBatch([], {active_executions: [], through_sequence: 1}),
    reconciliation: {kind: 'extend', historyFloor: 0},
  });

  expect(store.getState().activeExecutions).toEqual({});
});
