import {describe, expect, it} from 'vitest';
import type {RunEvent} from './protocol.js';
import {
  applyEvent,
  initialSessionState,
  selectNextAgent,
  selectNextRound,
  selectRound,
  visibleConversation,
  visiblePhases,
} from './session-model.js';

describe('session event model', () => {
  it('reduces semantic events into a presentation-neutral transcript', () => {
    let state = initialSessionState();
    state = applyEvent(
      state,
      event(1, 'phase_started', {
        kind: 'phase',
        phase: 'judge',
        attempt: 2,
      }),
    );
    state = applyEvent(
      state,
      event(2, 'agent_output_chunk', {
        kind: 'agent_output_chunk',
        channel: 'assistant',
        content: 'checking accuracy\n',
      }),
    );
    state = applyEvent(
      state,
      event(3, 'judge_result', {
        kind: 'judge_result',
        verdict: 'pass',
        feedback: '',
        attempt: 2,
      }),
    );

    expect(state.liveContent).toContain('[round-1] judge started');
    expect(state.liveContent).toContain('checking accuracy');
    expect(state.liveContent).toContain('Judge: PASS');
    expect(state.conversation.map(entry => entry.kind)).toEqual(['status', 'assistant', 'result']);
    expect(state.conversation[1]?.content).toBe('checking accuracy\n');
  });

  it('ignores replayed events and recognizes terminal state', () => {
    let state = applyEvent(initialSessionState(), event(4, 'run_finished'));
    state = applyEvent(state, event(3, 'run_failed'));

    expect(state.status).toBe('completed');
    expect(state.sequence).toBe(4);
    expect(state.terminal).toBe(true);
  });

  it('shows structured configuration failures as terminal conversation entries', () => {
    const state = applyEvent(
      initialSessionState(),
      event(1, 'configuration_failed', {
        kind: 'configuration_failed',
        code: 'resume_limit_exhausted',
        stage: 'resume_resolution',
        message: 'This run has completed 30 rounds.',
        usage: null,
        exit_code: 2,
      }),
    );

    expect(state.status).toBe('failed');
    expect(state.terminal).toBe(true);
    expect(state.overlay).toBeNull();
    expect(state.conversation[0]?.content).toContain('This run has completed 30 rounds.');
    expect(state.conversation[0]?.content).toContain('resume_limit_exhausted');
    expect(state.conversation[0]?.tone).toBe('failure');
  });

  it('coalesces streamed assistant chunks and pairs each tool call with its result', () => {
    let state = initialSessionState();
    state = applyEvent(
      state,
      event(
        1,
        'agent_output_chunk',
        {
          kind: 'agent_output_chunk',
          channel: 'assistant',
          content: 'hello ',
        },
        'invocation-1',
      ),
    );
    state = applyEvent(
      state,
      event(
        2,
        'agent_output_chunk',
        {
          kind: 'agent_output_chunk',
          channel: 'assistant',
          content: 'world',
        },
        'invocation-1',
      ),
    );
    state = applyEvent(
      state,
      event(
        3,
        'agent_output_chunk',
        {
          kind: 'agent_output_chunk',
          channel: 'tool',
          content: '→ Bash(command="first")\n',
        },
        'invocation-1',
      ),
    );
    state = applyEvent(
      state,
      event(
        4,
        'agent_output_chunk',
        {
          kind: 'agent_output_chunk',
          channel: 'tool',
          content: 'first result',
        },
        'invocation-1',
      ),
    );
    state = applyEvent(
      state,
      event(
        5,
        'agent_output_chunk',
        {
          kind: 'agent_output_chunk',
          channel: 'tool',
          content: '→ Bash(command="second")\n',
        },
        'invocation-1',
      ),
    );
    state = applyEvent(
      state,
      event(
        6,
        'agent_output_chunk',
        {
          kind: 'agent_output_chunk',
          channel: 'tool',
          content: 'second result',
        },
        'invocation-1',
      ),
    );

    expect(state.conversation.map(entry => entry.content)).toEqual([
      'hello world',
      '→ Bash(command="first")\nfirst result',
      '→ Bash(command="second")\nsecond result',
    ]);
    expect(state.conversation[1]).toMatchObject({
      toolCall: '→ Bash(command="first")\n',
      toolResponse: 'first result',
    });
  });

  it('classifies prompt events as distinct markdown turns', () => {
    const state = applyEvent(
      initialSessionState(),
      event(
        1,
        'agent_output_chunk',
        {
          kind: 'agent_output_chunk',
          channel: 'prompt',
          content: '# Task\n\nUse `pytest`.',
        },
        'invocation-1',
      ),
    );

    expect(state.conversation).toMatchObject([
      {
        kind: 'prompt',
        content: '# Task\n\nUse `pytest`.',
      },
    ]);
  });

  it('derives round-scoped agent flow from run and phase events', () => {
    let state = applyEvent(
      initialSessionState(),
      event(1, 'run_started', {
        kind: 'run_started',
        outer_loop: 'agent',
        input: 'examples/kv-store',
        max_rounds: 5,
      }),
    );
    expect(state.phases).toEqual([]);

    state = applyEvent(state, {
      ...event(2, 'phase_started', {kind: 'phase', phase: 'orchestrator', attempt: null}),
      agent_kind: 'orchestrator',
    });
    state = applyEvent(state, {
      ...event(3, 'phase_finished', {kind: 'phase', phase: 'orchestrator', attempt: null}),
      agent_kind: 'orchestrator',
    });

    expect(state.rounds).toMatchObject([{number: 1, status: 'active'}]);
    expect(visiblePhases(state).map(phase => `${phase.kind}:${phase.status}`)).toEqual([
      'orchestrator:completed',
      'implementer:pending',
      'judge:pending',
      'profiler:pending',
    ]);
    expect(visiblePhases(state)[0]).toMatchObject({
      kind: 'orchestrator',
      status: 'completed',
      roundNumber: 1,
      roundLabel: 'round-1',
    });
  });

  it('filters conversation entries by selected round and agent', () => {
    let state = initialSessionState();
    state = applyEvent(
      state,
      event(
        1,
        'agent_output_chunk',
        {
          kind: 'agent_output_chunk',
          channel: 'assistant',
          content: 'judge output',
        },
        'judge-1',
      ),
    );
    state = applyEvent(state, {
      ...event(
        2,
        'agent_output_chunk',
        {
          kind: 'agent_output_chunk',
          channel: 'assistant',
          content: 'profiler output',
        },
        'profiler-1',
      ),
      agent_kind: 'profiler',
    });
    state = applyEvent(state, {
      ...event(
        3,
        'agent_output_chunk',
        {
          kind: 'agent_output_chunk',
          channel: 'assistant',
          content: 'round two judge output',
        },
        'judge-2',
      ),
      round_label: 'round-2',
    });

    state = selectNextAgent(state);
    expect(state.selectedAgentKind).toBe('judge');
    expect(visibleConversation(state).map(entry => entry.content)).toEqual([
      'round two judge output',
    ]);
    state = selectNextRound(state);
    state = selectNextAgent(state);
    expect(visibleConversation(state).map(entry => entry.content)).toEqual(['judge output']);

    state = selectRound(state, 2);
    expect(visibleConversation(state).map(entry => entry.content)).toEqual([
      'round two judge output',
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
    ...(type === 'run_started' ? {} : {agent_kind: 'judge'}),
    ...(invocationId === undefined ? {} : {invocation_id: invocationId}),
    ...(data === undefined ? {} : {data}),
  };
}
