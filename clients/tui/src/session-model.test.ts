import {describe, expect, it} from 'vitest';
import type {RunEvent} from './protocol.js';
import {
  applyEvent,
  initialSessionState,
  selectNextAgent,
  selectNextRound,
  selectRound,
  statusText,
  toggleTodos,
  visibleConversation,
  visiblePhases,
  visibleTodos,
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

  it('pairs typed tool_call and tool_result events into tool turns', () => {
    let state = initialSessionState();
    state = applyEvent(
      state,
      event(1, 'tool_call', {kind: 'tool_call', tool: 'Bash', args: {command: 'first'}}, 'inv-1'),
    );
    state = applyEvent(
      state,
      event(
        2,
        'tool_result',
        {kind: 'tool_result', tool: 'Bash', content: 'first result'},
        'inv-1',
      ),
    );
    state = applyEvent(
      state,
      event(3, 'tool_call', {kind: 'tool_call', tool: 'Bash', args: {command: 'second'}}, 'inv-1'),
    );
    state = applyEvent(
      state,
      event(
        4,
        'tool_result',
        {kind: 'tool_result', tool: 'Bash', content: 'second result'},
        'inv-1',
      ),
    );

    expect(state.conversation.map(entry => entry.content)).toEqual([
      '→ Bash(command="first")\nfirst result',
      '→ Bash(command="second")\nsecond result',
    ]);
    expect(state.conversation[0]).toMatchObject({
      kind: 'tool',
      toolCall: '→ Bash(command="first")\n',
      toolResponse: 'first result',
    });
  });

  it('correlates parallel typed tool results by call ID', () => {
    let state = initialSessionState();
    state = applyEvent(
      state,
      event(
        1,
        'tool_call',
        {kind: 'tool_call', tool: 'Read', call_id: 'call-a', args: {path: 'a'}},
        'inv-1',
      ),
    );
    state = applyEvent(
      state,
      event(
        2,
        'tool_call',
        {kind: 'tool_call', tool: 'Read', call_id: 'call-b', args: {path: 'b'}},
        'inv-1',
      ),
    );
    state = applyEvent(
      state,
      event(
        3,
        'tool_result',
        {kind: 'tool_result', tool: 'Read', call_id: 'call-b', content: 'result b'},
        'inv-1',
      ),
    );
    state = applyEvent(
      state,
      event(
        4,
        'tool_result',
        {kind: 'tool_result', tool: 'Read', call_id: 'call-a', content: 'result a'},
        'inv-1',
      ),
    );

    expect(state.conversation.map(entry => entry.toolResponse)).toEqual(['result a', 'result b']);
  });

  it('truncates long typed tool-call arguments and renders non-string args as JSON', () => {
    const longArg = 'x'.repeat(200);
    const state = applyEvent(
      initialSessionState(),
      event(1, 'tool_call', {kind: 'tool_call', tool: 'Edit', args: {text: longArg, count: 3}}),
    );

    expect(state.conversation[0]?.toolCall).toContain(`text="${'x'.repeat(80)}..."`);
    expect(state.conversation[0]?.toolCall).toContain('count=3');
    expect(state.conversation[0]?.toolCall).not.toContain(longArg);
  });

  it('prefers typed tool events over legacy tool-channel chunks', () => {
    let state = initialSessionState();
    state = applyEvent(
      state,
      event(1, 'tool_call', {kind: 'tool_call', tool: 'Bash', args: {command: 'ls'}}, 'inv-1'),
    );
    // A legacy duplicate of the same call must not render a second turn.
    state = applyEvent(
      state,
      event(
        2,
        'agent_output_chunk',
        {kind: 'agent_output_chunk', channel: 'tool', content: '→ Bash(command="ls")\n'},
        'inv-1',
      ),
    );

    expect(state.typedToolEvents).toBe(true);
    expect(state.conversation).toHaveLength(1);
    expect(state.conversation[0]?.toolCall).toBe('→ Bash(command="ls")\n');
  });

  it('stores todo updates as per-phase data instead of transcript text', () => {
    const state = applyEvent(
      initialSessionState(),
      event(1, 'todo_update', {
        kind: 'todo_update',
        todos: [
          {content: 'Set up project', status: 'completed'},
          {content: 'Add tests', status: 'pending'},
        ],
      }),
    );

    expect(state.todoPhases).toEqual([
      {
        agentKind: 'judge',
        roundNumber: 1,
        items: [
          {content: 'Set up project', status: 'completed'},
          {content: 'Add tests', status: 'pending'},
        ],
      },
    ]);
    expect(state.conversation).toHaveLength(0);
  });

  it('keeps each phase’s todo list separate so agents never clobber each other', () => {
    let state = initialSessionState();
    state = applyEvent(state, {
      sequence: 1,
      timestamp: '2026-01-01T00:00:00Z',
      type: 'todo_update',
      agent_kind: 'implementer',
      round_label: 'round-1',
      data: {kind: 'todo_update', todos: [{content: 'Edit files', status: 'in_progress'}]},
    });
    state = applyEvent(state, {
      sequence: 2,
      timestamp: '2026-01-01T00:01:00Z',
      type: 'todo_update',
      agent_kind: 'judge',
      round_label: 'round-1',
      data: {kind: 'todo_update', todos: [{content: 'Check behavior', status: 'pending'}]},
    });
    state = applyEvent(state, {
      sequence: 3,
      timestamp: '2026-01-01T00:02:00Z',
      type: 'todo_update',
      agent_kind: 'implementer',
      round_label: 'round-2',
      data: {kind: 'todo_update', todos: [{content: 'Fix regression', status: 'pending'}]},
    });

    // Live view follows the currently active agent (round-2 implementer).
    expect(visibleTodos(state)).toEqual([{content: 'Fix regression', status: 'pending'}]);
    // Selecting a past agent shows that phase's final list, not the latest one.
    const withAgent = {...state, selectedRound: 1, selectedAgentKind: 'implementer'};
    expect(visibleTodos(withAgent)).toEqual([{content: 'Edit files', status: 'in_progress'}]);
    // Selecting only a round shows the round's most recently updated list.
    const withRound = {...state, selectedRound: 1};
    expect(visibleTodos(withRound)).toEqual([{content: 'Check behavior', status: 'pending'}]);
  });

  it('hides todos when the active phase has not emitted any', () => {
    let state = initialSessionState();
    state = applyEvent(state, {
      sequence: 1,
      timestamp: '2026-01-01T00:00:00Z',
      type: 'todo_update',
      agent_kind: 'implementer',
      round_label: 'round-1',
      data: {kind: 'todo_update', todos: [{content: 'Edit files', status: 'completed'}]},
    });
    // The judge phase starts without emitting todos; the implementer's
    // leftovers must not linger in the live view.
    state = applyEvent(state, {
      sequence: 2,
      timestamp: '2026-01-01T00:01:00Z',
      type: 'phase_started',
      agent_kind: 'judge',
      round_label: 'round-1',
    });

    expect(visibleTodos(state)).toEqual([]);
  });

  it('preserves unknown todo statuses for the renderer to degrade', () => {
    const state = applyEvent(
      initialSessionState(),
      event(1, 'todo_update', {
        kind: 'todo_update',
        todos: [{content: 'Mystery step', status: 'deferred'}],
      }),
    );

    expect(state.todoPhases[0]?.items).toEqual([{content: 'Mystery step', status: 'deferred'}]);
  });

  it('toggles the todo strip between collapsed and expanded', () => {
    const state = initialSessionState();
    expect(state.todosExpanded).toBe(false);
    expect(toggleTodos(state).todosExpanded).toBe(true);
    expect(toggleTodos(toggleTodos(state)).todosExpanded).toBe(false);
  });

  it('feeds usage updates into the status token meter', () => {
    let state = initialSessionState();
    expect(statusText(state)).not.toContain('tokens');
    state = applyEvent(
      state,
      event(1, 'usage_update', {
        kind: 'usage_update',
        input_tokens: 20_100,
        context_window: 1_000_000,
        model: 'claude-sonnet-4-6',
      }),
    );

    expect(state.usage).toEqual({
      inputTokens: 20_100,
      contextWindow: 1_000_000,
      model: 'claude-sonnet-4-6',
    });
    expect(statusText(state)).toContain('20k/1.0M tokens');
    expect(state.conversation).toHaveLength(0);
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
