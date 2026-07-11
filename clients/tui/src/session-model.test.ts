import {describe, expect, it} from 'vitest';
import {applyEvent, initialSessionState} from './session-model.js';
import type {RunEvent} from './protocol.js';

describe('session event model', () => {
  it('reduces semantic events into a presentation-neutral transcript', () => {
    let state = initialSessionState();
    state = applyEvent(state, event(1, 'phase_started', {
      kind: 'phase', phase: 'judge', attempt: 2,
    }));
    state = applyEvent(state, event(2, 'agent_output_chunk', {
      kind: 'agent_output_chunk', channel: 'assistant', content: 'checking accuracy\n',
    }));
    state = applyEvent(state, event(3, 'judge_result', {
      kind: 'judge_result', verdict: 'pass', feedback: '', attempt: 2,
    }));

    expect(state.liveContent).toContain('[round-1] judge started');
    expect(state.liveContent).toContain('checking accuracy');
    expect(state.liveContent).toContain('Judge: PASS');
    expect(state.conversation.map(entry => entry.kind)).toEqual([
      'status', 'assistant', 'result',
    ]);
    expect(state.conversation[1]?.content).toBe('checking accuracy\n');
  });

  it('ignores replayed events and recognizes terminal state', () => {
    let state = applyEvent(initialSessionState(), event(4, 'run_finished'));
    state = applyEvent(state, event(3, 'run_failed'));

    expect(state.status).toBe('completed');
    expect(state.sequence).toBe(4);
    expect(state.terminal).toBe(true);
  });

  it('coalesces streamed assistant chunks but keeps tool turns separate', () => {
    let state = initialSessionState();
    state = applyEvent(state, event(1, 'agent_output_chunk', {
      kind: 'agent_output_chunk', channel: 'assistant', content: 'hello ',
    }, 'invocation-1'));
    state = applyEvent(state, event(2, 'agent_output_chunk', {
      kind: 'agent_output_chunk', channel: 'assistant', content: 'world',
    }, 'invocation-1'));
    state = applyEvent(state, event(3, 'agent_output_chunk', {
      kind: 'agent_output_chunk', channel: 'tool', content: 'first tool',
    }, 'invocation-1'));
    state = applyEvent(state, event(4, 'agent_output_chunk', {
      kind: 'agent_output_chunk', channel: 'tool', content: 'second tool',
    }, 'invocation-1'));

    expect(state.conversation.map(entry => entry.content)).toEqual([
      'hello world', 'first tool', 'second tool',
    ]);
  });
});

function event(
  sequence: number,
  type: RunEvent['type'],
  data?: RunEvent['data'],
  invocationId?: string,
): RunEvent {
  return {
    sequence,
    timestamp: '2026-01-01T00:00:00Z',
    type,
    round_label: 'round-1',
    agent_kind: 'judge',
    ...(invocationId === undefined ? {} : {invocation_id: invocationId}),
    ...(data === undefined ? {} : {data}),
  };
}
