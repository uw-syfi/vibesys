import {describe, expect, it} from 'bun:test';
import type {RunEvent, RunSnapshot} from '@vibesys/backend-client';
import {
  type CoreRunStatus,
  type CoreState,
  DEFAULT_CHAT_THREAD_ID,
  hasRunEnded,
  initialCoreState,
  latestDiagnosticChange,
  reconcileActiveExecutions,
  reduceEvent,
  reduceEventBatch,
  reduceEventRebootstrap,
  reduceSnapshot,
} from './core-state.js';

describe('core state projection', () => {
  it('projects snapshots without changing event-derived history', () => {
    const prior = reduceEvent(initialCoreState(), outputEvent(4, 'kept'));
    const snapshot = {
      run_id: 'run',
      status: 'running',
      sequence: 9,
      agent_kind: 'judge',
      round_label: 'round-2-judge',
      active_executions: [checkpoint('exec-1')],
    } satisfies RunSnapshot;

    const state = reduceSnapshot(prior, snapshot);

    expect(state.sequence).toBe(4);
    expect(state.transcript.map(entry => entry.content)).toEqual(['kept']);
    expect(state.agentKind).toBe('judge');
    expect(state.activeExecutions['exec-1']?.roundNumber).toBe(2);
  });

  it('rejects a snapshot older than the projected event cursor', () => {
    const current = reduceEvent(initialCoreState(), outputEvent(5, 'current'));
    const stale = {
      run_id: 'run',
      status: 'running',
      sequence: 4,
      agent_kind: 'judge',
      round_label: 'round-2-judge',
      active_executions: [checkpoint('stale')],
    } satisfies RunSnapshot;

    expect(reduceSnapshot(current, stale)).toBe(current);
  });

  it('registers the chat threads a snapshot projects', () => {
    const state = reduceSnapshot(initialCoreState(), {
      run_id: 'run',
      status: 'running',
      sequence: 1,
      chat_threads: [
        {
          thread_id: 'thread-a',
          title: 'Ring buffer sizing',
          driver: 'agentshim',
          provider: 'anthropic',
          model: 'opus',
        },
      ],
    } satisfies RunSnapshot);

    expect(state.chatThreads).toEqual([
      {id: DEFAULT_CHAT_THREAD_ID, title: '', driver: null, provider: null, model: null},
      {
        id: 'thread-a',
        title: 'Ring buffer sizing',
        driver: 'agentshim',
        provider: 'anthropic',
        model: 'opus',
      },
    ]);
    expect(state.chatTranscripts['thread-a']).toEqual([]);
  });

  // Boot issues the snapshot query and the subscription concurrently, and under
  // a tail bootstrap the replay batch usually lands first. The registry is a
  // fact about history already written, so the liveness guard must not drop it.
  it('registers projected chat threads even from a stale snapshot', () => {
    const current = reduceEvent(initialCoreState(), outputEvent(5, 'current'));

    const state = reduceSnapshot(current, {
      run_id: 'run',
      status: 'running',
      sequence: 4,
      chat_threads: [
        {thread_id: 'thread-a', title: '', driver: 'agentshim', provider: 'codex', model: 'gpt-5'},
      ],
    } satisfies RunSnapshot);

    expect(state.status).toBe(current.status);
    expect(state.chatThreads.map(thread => thread.id)).toEqual([
      DEFAULT_CHAT_THREAD_ID,
      'thread-a',
    ]);
  });

  it('leaves a stale snapshot that projects no chat threads identity-preserving', () => {
    const current = reduceEvent(initialCoreState(), outputEvent(5, 'current'));
    const stale = {run_id: 'run', status: 'running', sequence: 4} satisfies RunSnapshot;

    expect(reduceSnapshot(current, stale)).toBe(current);
  });

  it('merges projected chat threads with replayed ones without duplicating', () => {
    let current = reduceEvent(initialCoreState(), threadCreatedEvent(1, 'thread-a', 'anthropic'));
    current = reduceEvent(current, chatTitledEvent(2, 'thread-a', 'Replayed title'));

    const state = reduceSnapshot(current, {
      run_id: 'run',
      status: 'running',
      sequence: 3,
      chat_threads: [
        {
          thread_id: 'thread-a',
          title: '',
          driver: 'agentshim',
          provider: 'anthropic',
          model: 'opus',
        },
        {
          thread_id: 'thread-b',
          title: 'Projected',
          driver: 'agentshim',
          provider: 'codex',
          model: 'gpt-5',
        },
      ],
    } satisfies RunSnapshot);

    expect(state.chatThreads).toEqual([
      {id: DEFAULT_CHAT_THREAD_ID, title: '', driver: null, provider: null, model: null},
      {
        id: 'thread-a',
        title: 'Replayed title',
        driver: 'agentshim',
        provider: 'anthropic',
        model: 'opus',
      },
      {
        id: 'thread-b',
        title: 'Projected',
        driver: 'agentshim',
        provider: 'codex',
        model: 'gpt-5',
      },
    ]);
  });

  it('ignores duplicate replay events', () => {
    const once = reduceEvent(initialCoreState(), outputEvent(1, 'one'));
    const replayed = reduceEvent(once, outputEvent(1, 'duplicate'));

    expect(replayed).toBe(once);
    expect(replayed.transcript.map(entry => entry.content)).toEqual(['one']);
  });

  it('preserves published run-map arrays through core-state clone paths', () => {
    const started = reduceEvent(
      initialCoreState(),
      executionEvent(1, 'agent_execution_started', 'exec', startedData('Implement')),
    );
    const rounds = started.rounds;
    const phases = started.phases;

    const streamed = reduceEvent(started, outputEvent(2, 'working', 'exec'));
    const diagnosed = reduceEvent(
      streamed,
      diagnosticEvent(3, 'agent_output_chunk', 'warning', 'warning', 'Check output'),
    );
    const batched = reduceEventBatch(diagnosed, [outputEvent(4, 'still working', 'exec')]);
    const chatted = reduceEvent(batched, chatAnswerEvent(5, 'status'));

    expect(streamed.rounds).toBe(rounds);
    expect(streamed.phases).toBe(phases);
    expect(diagnosed.rounds).toBe(rounds);
    expect(diagnosed.phases).toBe(phases);
    expect(batched.rounds).toBe(rounds);
    expect(batched.phases).toBe(phases);
    expect(chatted.rounds).toBe(rounds);
    expect(chatted.phases).toBe(phases);
  });

  it('applies event batches before reconciling their execution checkpoint', () => {
    const started = executionEvent(1, 'agent_execution_started', 'stale', {
      kind: 'agent_execution_started',
      stage: 'implementation',
      attempt: 1,
      system_prompt: '',
      user_prompt: 'Implement the queue',
      activity: {
        kind: 'agent_execution_activity_changed',
        mode: 'thinking',
        summary: 'Inspecting',
        tool: null,
      },
    });

    const state = reduceEventBatch(initialCoreState(), [started, outputEvent(2, 'done')], [], 2);

    expect(state.sequence).toBe(2);
    expect(state.activeExecutions).toEqual({});
    expect(state.transcript.at(-1)?.content).toBe('done');
  });

  it('rejects an older execution checkpoint', () => {
    const current = reduceEvent(initialCoreState(), outputEvent(5, 'current'));

    expect(reconcileActiveExecutions(current, [checkpoint('stale')], 4)).toBe(current);
  });

  it('tracks concurrent executions independently through activity and completion', () => {
    let state = initialCoreState();
    state = reduceEvent(
      state,
      executionEvent(1, 'agent_execution_started', 'first', startedData('First')),
    );
    state = reduceEvent(
      state,
      executionEvent(2, 'agent_execution_started', 'second', startedData('Second')),
    );
    state = reduceEvent(
      state,
      executionEvent(3, 'agent_execution_activity_changed', 'second', {
        kind: 'agent_execution_activity_changed',
        mode: 'tool',
        summary: 'Running tests',
        tool: 'Bash',
      }),
    );
    state = reduceEvent(
      state,
      executionEvent(4, 'agent_execution_finished', 'first', {
        kind: 'agent_execution_finished',
        error: null,
      }),
    );

    expect(Object.keys(state.activeExecutions)).toEqual(['second']);
    expect(state.activeExecutions['second']?.activity).toEqual({
      mode: 'tool',
      summary: 'Running tests',
      tool: 'Bash',
    });
  });

  it('captures runtime identity from agent_execution_started when present', () => {
    const state = reduceEvent(
      initialCoreState(),
      executionEvent(1, 'agent_execution_started', 'first', {
        ...startedData('Implement the queue'),
        driver: 'agentshim',
        provider: 'codex',
        model: 'gpt-5.1-codex-max',
      }),
    );

    expect(state.activeExecutions['first']).toMatchObject({
      driver: 'agentshim',
      provider: 'codex',
      model: 'gpt-5.1-codex-max',
    });
  });

  it('defaults runtime identity to null when the event omits it', () => {
    const state = reduceEvent(
      initialCoreState(),
      executionEvent(1, 'agent_execution_started', 'first', startedData('Implement the queue')),
    );

    expect(state.activeExecutions['first']).toMatchObject({
      driver: null,
      provider: null,
      model: null,
    });
  });

  // A round drilldown's activity bar sources a running agent's harness/model
  // from this checkpoint, not only from the live agent_execution_started
  // event: the checkpoint replaces the whole activeExecutions record (see
  // reconcileActiveExecutions), so if it dropped the identity fields, any
  // event_batch or reconnect would erase a label the live event had just set.
  it('carries runtime identity through a snapshot checkpoint', () => {
    const snapshot = {
      run_id: 'run',
      status: 'running',
      sequence: 1,
      agent_kind: 'judge',
      round_label: 'round-2-judge',
      active_executions: [
        checkpoint('exec-1', {driver: 'agentshim', provider: 'codex', model: 'gpt-5.1-codex-max'}),
      ],
    } satisfies RunSnapshot;

    const state = reduceSnapshot(initialCoreState(), snapshot);

    expect(state.activeExecutions['exec-1']).toMatchObject({
      driver: 'agentshim',
      provider: 'codex',
      model: 'gpt-5.1-codex-max',
    });
  });

  it('defaults runtime identity to null for a checkpoint that omits it', () => {
    const state = reconcileActiveExecutions(initialCoreState(), [checkpoint('exec-1')]);

    expect(state.activeExecutions['exec-1']).toMatchObject({
      driver: null,
      provider: null,
      model: null,
    });
  });

  it('coalesces streamed assistant chunks by invocation', () => {
    let state = initialCoreState();
    state = reduceEvent(state, outputEvent(1, 'hello ', 'turn-1'));
    state = reduceEvent(state, outputEvent(2, 'world', 'turn-1'));
    state = reduceEvent(state, outputEvent(3, 'separate', 'turn-2'));

    expect(state.transcript.map(entry => entry.content)).toEqual(['hello world', 'separate']);
  });

  it('correlates parallel tool results by call id', () => {
    let state = initialCoreState();
    state = reduceEvent(state, toolEvent(1, 'tool_call', 'call-a', 'first'));
    state = reduceEvent(state, toolEvent(2, 'tool_call', 'call-b', 'second'));
    state = reduceEvent(state, toolEvent(3, 'tool_result', 'call-b', 'second result'));
    state = reduceEvent(state, toolEvent(4, 'tool_result', 'call-a', 'first result'));

    expect(state.transcript).toHaveLength(2);
    expect(state.transcript[0]?.toolResult?.content).toBe('first result');
    expect(state.transcript[1]?.toolResult?.content).toBe('second result');
  });

  it('retains typed tool arguments and results without presentation loss', () => {
    const arguments_ = {
      text: 'x'.repeat(200),
      nested: {items: [1, {enabled: true, labels: ['alpha', 'beta']}]},
    };
    let state = reduceEvent(initialCoreState(), {
      ...baseEvent(1, 'tool_call'),
      invocation_id: 'turn',
      data: {kind: 'tool_call', tool: 'Edit', call_id: 'call-long', args: arguments_},
    });
    state = reduceEvent(state, {
      ...baseEvent(2, 'tool_result'),
      invocation_id: 'turn',
      data: {
        kind: 'tool_result',
        tool: 'Edit',
        call_id: 'call-long',
        content: 'result '.repeat(40),
        is_error: true,
      },
    });

    expect(state.transcript[0]?.toolArguments).toEqual(arguments_);
    expect(state.transcript[0]?.toolResult).toEqual({
      kind: 'tool_result',
      tool: 'Edit',
      call_id: 'call-long',
      content: 'result '.repeat(40),
      is_error: true,
    });
    expect(state.transcript[0]?.toolCall).toBeUndefined();
    expect(state.transcript[0]?.toolResponse).toBeUndefined();
  });

  it('carries the typed result payload onto the merged transcript entry', () => {
    let state = reduceEvent(initialCoreState(), {
      ...baseEvent(1, 'tool_call'),
      invocation_id: 'turn',
      data: {kind: 'tool_call', tool: 'shell', call_id: 'call-1', args: {cmd: 'ls'}},
    });
    state = reduceEvent(state, {
      ...baseEvent(2, 'tool_result'),
      invocation_id: 'turn',
      data: {
        kind: 'tool_result',
        tool: 'shell',
        call_id: 'call-1',
        content: 'file.txt',
        payload: {kind: 'command', stdout: 'file.txt', stderr: '', exit_code: 0, duration: 0.1},
      },
    });

    expect(state.transcript).toHaveLength(1);
    expect(state.transcript[0]?.toolResult?.payload).toEqual({
      kind: 'command',
      stdout: 'file.txt',
      stderr: '',
      exit_code: 0,
      duration: 0.1,
    });
  });

  it('keeps chat-agent events out of the experiment transcript', () => {
    const chat = {
      ...outputEvent(1, 'answer'),
      agent_kind: 'chat',
      round_label: 'experiment-chat',
    } satisfies RunEvent;

    const state = reduceEvent(initialCoreState(), chat);

    expect(state.transcript).toEqual([]);
    expect(state.chatTranscript.map(entry => entry.content)).toEqual(['answer']);
  });

  it('partitions chat transcripts by thread, defaulting unstamped events', () => {
    let state = initialCoreState();
    state = reduceEvent(state, chatAnswerEvent(1, 'default answer'));
    state = reduceEvent(state, chatAnswerEvent(2, 'thread answer', 'thread-a'));

    // Neither thread sees the other's answer, and unstamped events land on
    // the default thread so pre-thread logs replay unchanged.
    expect(state.chatTranscripts['default']?.map(entry => entry.content)).toEqual([
      'default answer',
    ]);
    expect(state.chatTranscripts['thread-a']?.map(entry => entry.content)).toEqual([
      'thread answer',
    ]);
    // The legacy selector still reads the default thread.
    expect(state.chatTranscript.map(entry => entry.content)).toEqual(['default answer']);
    expect(state.transcript).toEqual([]);
  });

  it('folds the final chat answer over its own streamed chunks', () => {
    let state = initialCoreState();
    state = reduceEvent(state, chatStreamEvent(1, 'The queue ', 'exec-1'));
    state = reduceEvent(state, chatStreamEvent(2, 'is lock-free.', 'exec-1'));
    state = reduceEvent(state, chatAnswerEvent(3, 'The queue is lock-free.'));

    // One answer block, under the streamed entry's id so consumers tracking
    // entries by id update in place, closed to any further folding.
    expect(state.chatTranscript).toHaveLength(1);
    expect(state.chatTranscript[0]).toMatchObject({
      id: '1',
      kind: 'assistant',
      label: 'Answer',
      content: 'The queue is lock-free.',
    });
    expect(state.chatTranscript[0]?.turnId).toBeUndefined();
  });

  it('reconciles each chat turn separately and appends unstreamed answers', () => {
    const events = [
      chatStreamEvent(1, 'first ', 'exec-1'),
      chatStreamEvent(2, 'answer', 'exec-1'),
      chatAnswerEvent(3, 'first answer'),
      chatAnswerEvent(4, 'unstreamed answer'),
      chatStreamEvent(5, 'second answer', 'exec-2'),
      chatAnswerEvent(6, 'second answer'),
    ];
    const batched = reduceEventBatch(initialCoreState(), events);
    let single = initialCoreState();
    for (const item of events) single = reduceEvent(single, item);

    // Both fold paths agree: a finalized turn cannot swallow the next answer,
    // and an answer that never streamed still lands as its own entry.
    for (const state of [batched, single]) {
      expect(state.chatTranscript.map(item => [item.id, item.content])).toEqual([
        ['1', 'first answer'],
        ['4', 'unstreamed answer'],
        ['5', 'second answer'],
      ]);
      expect(state.chatTranscript.every(item => item.turnId === undefined)).toBe(true);
    }
  });

  it('replays the thread list from creation events after the implicit default', () => {
    let state = initialCoreState();
    state = reduceEvent(state, threadCreatedEvent(1, 'thread-a', 'claude'));
    state = reduceEvent(state, threadCreatedEvent(2, 'thread-b', 'codex'));

    expect(state.chatThreads.map(thread => thread.id)).toEqual(['default', 'thread-a', 'thread-b']);
    // The implicit default carries no backend title; consumers name it.
    expect(state.chatThreads[0]).toMatchObject({title: '', driver: null, provider: null});
    expect(state.chatThreads[1]).toMatchObject({
      title: '',
      driver: 'agentshim',
      provider: 'claude',
      model: 'opus',
    });
    // A created thread has a transcript from the start, even before it talks.
    expect(state.chatTranscripts['thread-b']).toEqual([]);
  });

  it('adopts the backend-derived title carried on a chat event', () => {
    let state = initialCoreState();
    state = reduceEvent(state, threadCreatedEvent(1, 'thread-a', 'claude'));
    state = reduceEvent(state, {
      ...chatAnswerEvent(2, 'first answer', 'thread-a'),
      data: {kind: 'chat', answer: 'first answer', thread_title: 'why did r2 regress'},
    });

    expect(state.chatThreads.find(thread => thread.id === 'thread-a')?.title).toBe(
      'why did r2 regress',
    );
  });

  it('names a thread from a titled turn even when its creation replayed away', () => {
    const state = reduceEvent(initialCoreState(), {
      ...chatAnswerEvent(1, 'answer', 'thread-x'),
      data: {kind: 'chat', answer: 'answer', thread_title: 'orphan thread'},
    });

    expect(state.chatThreads.find(thread => thread.id === 'thread-x')?.title).toBe('orphan thread');
    expect(state.chatTranscripts['thread-x']?.map(entry => entry.content)).toEqual(['answer']);
  });

  it('drops legacy chat tool chunks per thread once typed events appear', () => {
    let state = initialCoreState();
    state = reduceEvent(state, {
      ...baseEvent(1, 'tool_call'),
      agent_kind: 'chat',
      chat_thread_id: 'thread-a',
      data: {kind: 'tool_call', tool: 'read_file', args: {}},
    });
    // The default thread saw no typed events, so its legacy chunks survive.
    state = reduceEvent(state, {
      ...outputEvent(2, 'legacy default output'),
      agent_kind: 'chat',
    });

    expect(state.chatTypedToolEvents).toEqual({'thread-a': true});
    expect(state.chatTranscript.map(entry => entry.content)).toEqual(['legacy default output']);
  });

  it('scopes todo snapshots by execution', () => {
    let state = initialCoreState();
    state = reduceEvent(state, todoEvent(1, 'exec-a', 'first'));
    state = reduceEvent(state, todoEvent(2, 'exec-b', 'second'));
    state = reduceEvent(state, todoEvent(3, 'exec-a', 'updated'));

    expect(state.todos).toMatchObject([
      {executionId: 'exec-b', items: [{content: 'second'}]},
      {executionId: 'exec-a', items: [{content: 'updated'}]},
    ]);
  });

  it('retains semantic benchmark data independently of rendered charts', () => {
    const state = reduceEvent(initialCoreState(), {
      ...baseEvent(8, 'benchmark_result'),
      data: {kind: 'benchmark_result', metric: 'ops', value: 42, unit: 'ops/s'},
    });

    expect(state.benchmarks).toEqual([
      {sequence: 8, roundNumber: 1, metric: 'ops', value: 42, unit: 'ops/s'},
    ]);
  });

  it('records structured diagnostics as durable facts', () => {
    const state = reduceEvent(initialCoreState(), {
      ...baseEvent(3, 'run_failed'),
      diagnostic: {
        id: 'diag-1',
        code: 'agent_failed',
        summary: 'Agent failed.',
        detail: 'Exit 2',
        hint: 'Retry.',
        scope: 'run',
        severity: 'fatal',
        retryability: 'manual',
        cause_id: null,
        debug_ref: null,
      },
    });

    expect(hasRunEnded(state)).toBe(true);
    expect(state.diagnostics).toMatchObject([
      {id: 'diag-1', summary: 'Agent failed.', severity: 'fatal', sequence: 3},
    ]);
  });

  it('promotes a repeated diagnostic id with richer terminal detail', () => {
    const initialFailure = reduceEvent(
      initialCoreState(),
      diagnosticEvent(1, 'invocation_finished', 'diag-1', 'error', 'Agent failed.', {
        invocationId: 'invocation-1',
        detail: null,
      }),
    );
    const state = reduceEvent(
      initialFailure,
      diagnosticEvent(2, 'run_failed', 'diag-1', 'fatal', 'Agent failed terminally.', {
        detail: 'Exit 2',
      }),
    );

    expect(state.diagnostics).toHaveLength(1);
    const updatedDiagnostic = state.diagnostics[0];
    if (updatedDiagnostic === undefined) throw new Error('Expected a projected diagnostic');
    expect(updatedDiagnostic).toMatchObject({
      id: 'diag-1',
      summary: 'Agent failed terminally.',
      detail: 'Exit 2',
      severity: 'fatal',
      invocationId: 'invocation-1',
      sequence: 2,
    });
    expect(latestDiagnosticChange(initialFailure, state)).toBe(updatedDiagnostic);
  });

  it('preserves distinct diagnostic ids from the same invocation', () => {
    let state = reduceEvent(
      initialCoreState(),
      diagnosticEvent(1, 'invocation_finished', 'diag-1', 'error', 'First failure.', {
        invocationId: 'invocation-1',
      }),
    );
    state = reduceEvent(
      state,
      diagnosticEvent(2, 'phase_finished', 'diag-2', 'error', 'Second failure.', {
        invocationId: 'invocation-1',
      }),
    );

    expect(state.diagnostics.map(diagnostic => diagnostic.id)).toEqual(['diag-1', 'diag-2']);
  });

  it('retains structured configuration failure detail in the transcript', () => {
    const state = reduceEvent(initialCoreState(), {
      ...baseEvent(3, 'configuration_failed'),
      data: {
        kind: 'configuration_failed',
        code: 'resume_limit_exhausted',
        message: 'This run has completed 30 rounds.',
        usage: 'Use a larger limit.',
        stage: 'configuration',
        exit_code: 2,
      },
    });

    expect(state.transcript[0]?.content).toContain('resume_limit_exhausted');
    expect(state.transcript[0]?.content).toContain('Use a larger limit.');
    expect(state.diagnostics[0]).toMatchObject({
      failureKind: 'configuration',
      summary:
        'This run has completed 30 rounds.\n\nUse a larger limit.\n\nCode: resume_limit_exhausted · Stage: configuration',
    });
  });

  it('projects an interruption discriminator without presentation labels', () => {
    const state = reduceEvent(initialCoreState(), {
      ...baseEvent(3, 'run_interrupted'),
      data: {kind: 'run_interrupted', reason: 'launcher_terminated', signal: 'SIGTERM'},
    });

    expect(state.diagnostics[0]).toMatchObject({
      failureKind: 'run_interruption',
      scope: 'run',
      summary: 'launcher_terminated (SIGTERM)',
      severity: 'fatal',
    });
    expect('title' in (state.diagnostics[0] ?? {})).toBe(false);
  });

  it('distinguishes failed and interrupted terminal transcript entries', () => {
    const failed = reduceEvent(initialCoreState(), {
      ...baseEvent(1, 'run_failed'),
      text: '',
    });
    const interrupted = reduceEvent(initialCoreState(), {
      ...baseEvent(1, 'run_interrupted'),
      text: '',
      data: {kind: 'run_interrupted', reason: 'Operator stopped the run', signal: 'SIGINT'},
    });

    expect(failed.transcript.at(-1)).toMatchObject({
      content: 'Run failed.',
      label: 'Run failed',
    });
    expect(interrupted.transcript.at(-1)).toMatchObject({
      content: 'Operator stopped the run (SIGINT)',
      label: 'Run interrupted',
    });
  });

  it('exposes experiment changes only as stream-derived invalidation', () => {
    const state = reduceEvent(initialCoreState(), {
      ...baseEvent(12, 'experiments_changed'),
      data: {kind: 'experiments_changed', reason: 'round_persisted'},
    });

    expect(state.experimentsRevision).toBe(12);
    expect('experimentLog' in state).toBe(false);
  });
});

describe('whether a run has ended', () => {
  const withStatus = (status: CoreRunStatus): CoreState => ({...initialCoreState(), status});

  it('classifies every run status the projection can hold', () => {
    expect(hasRunEnded(withStatus('completed'))).toBe(true);
    expect(hasRunEnded(withStatus('failed'))).toBe(true);
    expect(hasRunEnded(withStatus('connecting'))).toBe(false);
    expect(hasRunEnded(withStatus('starting'))).toBe(false);
    expect(hasRunEnded(withStatus('running'))).toBe(false);
    expect(hasRunEnded(withStatus('pausing'))).toBe(false);
    expect(hasRunEnded(withStatus('paused'))).toBe(false);
  });

  it('reads an ended run from a bootstrapped snapshot', () => {
    const snapshot = {run_id: 'run', status: 'completed', sequence: 4} satisfies RunSnapshot;

    const state = reduceSnapshot(initialCoreState(), snapshot);

    expect(hasRunEnded(state)).toBe(true);
  });

  // A resumed run replays the previous process's failure ahead of its own
  // start. Whether the run has ended is derived from the status, so the later
  // `run_started` cannot leave the projection looking finished while the run
  // is live.
  it('has not ended after a resumed run replays a failure then a start', () => {
    const state = reduceEventBatch(initialCoreState(), [
      baseEvent(1, 'run_failed'),
      {
        ...baseEvent(2, 'run_started'),
        data: {kind: 'run_started', outer_loop: 'agent', input: '.', max_rounds: 3},
      },
    ]);

    expect(state.status).toBe('running');
    expect(hasRunEnded(state)).toBe(false);
  });
});

// The backend owns the run lifecycle and publishes every move through it. The
// projection folds those events and holds no lifecycle flag of its own, so a
// pause is visible for exactly as long as the backend says it lasts.
describe('the run lifecycle', () => {
  const statusEvent = (sequence: number, status: CoreRunStatus, previous: CoreRunStatus) =>
    ({
      ...baseEvent(sequence, 'run_status_changed'),
      data: {kind: 'run_status_changed', status, previous},
    }) as RunEvent;

  it('folds a pause request, its boundary, and the resume', () => {
    const requested = reduceEvent(initialCoreState(), statusEvent(1, 'pausing', 'running'));
    expect(requested.status).toBe('pausing');

    const paused = reduceEvent(requested, statusEvent(2, 'paused', 'pausing'));
    expect(paused.status).toBe('paused');

    const resumed = reduceEvent(paused, statusEvent(3, 'running', 'paused'));
    expect(resumed.status).toBe('running');
    expect(hasRunEnded(resumed)).toBe(false);
  });

  it('ends a run that was paused when it stopped', () => {
    const state = reduceEventBatch(initialCoreState(), [
      statusEvent(1, 'pausing', 'running'),
      statusEvent(2, 'paused', 'pausing'),
      statusEvent(3, 'completed', 'paused'),
    ]);

    expect(state.status).toBe('completed');
    expect(hasRunEnded(state)).toBe(true);
  });

  it('drops the active executions of a run that ended while paused', () => {
    const running = reduceSnapshot(initialCoreState(), {
      run_id: 'run',
      status: 'paused',
      sequence: 1,
      active_executions: [checkpoint('exec-1')],
    } satisfies RunSnapshot);

    const state = reduceEvent(running, statusEvent(2, 'failed', 'paused'));

    expect(state.activeExecutions).toEqual({});
  });

  it('keeps an ended run ended against a snapshot no newer than the fold', () => {
    const ended = reduceEventBatch(initialCoreState(), [
      statusEvent(1, 'pausing', 'running'),
      statusEvent(2, 'completed', 'pausing'),
      baseEvent(3, 'run_finished'),
    ]);

    const stale = reduceSnapshot(ended, {
      run_id: 'run',
      status: 'running',
      sequence: 3,
    } satisfies RunSnapshot);

    expect(stale.status).toBe('completed');
  });

  it('reads a resumed run as running from the transition after the replay', () => {
    const state = reduceEventBatch(initialCoreState(), [
      statusEvent(1, 'completed', 'running'),
      baseEvent(2, 'run_finished'),
      statusEvent(3, 'running', 'starting'),
    ]);

    expect(state.status).toBe('running');
    expect(hasRunEnded(state)).toBe(false);
  });
});

// The run's durable event log is attached after a client subscribes, so a
// subscription bootstrapped against the server's own short log is later
// re-bootstrapped at a tail of the run log. The two batches number different
// logs, which is why the second supersedes the state the first built.
describe('a re-bootstrapped stream', () => {
  const runLog: RunEvent[] = [
    {
      ...baseEvent(1, 'run_started'),
      data: {kind: 'run_started', outer_loop: 'agent', input: '.', max_rounds: 3},
    },
    outputEvent(2, 'two'),
    outputEvent(3, 'three'),
  ];

  it('folds events the superseded cursor would have dropped', () => {
    const superseded = reduceEventBatch(initialCoreState(), [outputEvent(2, 'pre-attach')]);

    const state = reduceEventRebootstrap(superseded, runLog, [], 3, 1);

    expect(state.maxRounds).toBe(3);
    expect(state.outerLoop).toBe('agent');
    // One turn, so the two chunks concatenate; the superseded 'pre-attach'
    // chunk is gone rather than concatenated onto them.
    expect(state.transcript.map(entry => entry.content)).toEqual(['twothree']);
    expect(state.historyAfterSequence).toBe(1);
  });

  it('keeps the chat threads a concurrent snapshot registered', () => {
    const superseded = reduceSnapshot(initialCoreState(), {
      run_id: 'run',
      status: 'running',
      sequence: 1,
      chat_threads: [
        {
          thread_id: 'thread-a',
          title: 'Ring buffer sizing',
          driver: 'agentshim',
          provider: 'anthropic',
          model: 'opus',
        },
      ],
    } satisfies RunSnapshot);

    const state = reduceEventRebootstrap(superseded, runLog, [], 3, 1);

    expect(state.chatThreads.map(thread => thread.id)).toEqual([
      DEFAULT_CHAT_THREAD_ID,
      'thread-a',
    ]);
  });
});

// A batch folds its transcripts in one working array instead of copying them
// per event. That is only sound while it stays indistinguishable from folding
// the same events one at a time, which is what these pin.
describe('batched transcript folding', () => {
  it('folds a batch exactly like folding its events one at a time', () => {
    const events = mixedTranscriptEvents();

    expect(reduceEventBatch(initialCoreState(), events)).toEqual(
      events.reduce(reduceEvent, initialCoreState()),
    );
  });

  it('correlates interleaved tool results by call id within one batch', () => {
    const events = [
      toolEvent(1, 'tool_call', 'call-a', 'first'),
      toolEvent(2, 'tool_call', 'call-b', 'second'),
      toolEvent(3, 'tool_result', 'call-b', 'second result'),
      toolEvent(4, 'tool_result', 'call-a', 'first result'),
    ];

    const state = reduceEventBatch(initialCoreState(), events);

    expect(state.transcript).toHaveLength(2);
    expect(state.transcript[0]?.toolResult?.content).toBe('first result');
    expect(state.transcript[1]?.toolResult?.content).toBe('second result');
  });

  it('merges a result without a call id into the oldest open call of that tool', () => {
    const events = [
      toolEvent(1, 'tool_call', 'call-a', 'first'),
      toolEvent(2, 'tool_call', 'call-b', 'second'),
      {
        ...baseEvent(3, 'tool_result'),
        invocation_id: 'turn',
        data: {kind: 'tool_result', tool: 'Bash', content: 'anonymous result', is_error: false},
      } satisfies RunEvent,
    ];

    const state = reduceEventBatch(initialCoreState(), events);

    expect(state.transcript).toHaveLength(2);
    expect(state.transcript[0]?.content).toContain('anonymous result');
    expect(state.transcript[1]?.toolResult).toBeUndefined();
    expect(state).toEqual(events.reduce(reduceEvent, initialCoreState()));
  });

  it('evicts the oldest round whole when a batch passes the transcript cap', () => {
    const events = [
      ...Array.from({length: 10_000}, (_, index) => roundOutputEvent(index + 1, 1)),
      roundToolEvent(10_001, 'tool_call', 'call-late', 'survivor'),
      ...Array.from({length: 10_000}, (_, index) => roundOutputEvent(10_002 + index, 2)),
      roundToolEvent(20_002, 'tool_result', 'call-late', 'late result'),
    ];

    const state = reduceEventBatch(initialCoreState(), events);

    // Round 1 goes as a block; the surviving round-2 tool call still merges its
    // result, so the open-call index survived the eviction.
    expect(state.transcript).toHaveLength(10_001);
    expect(state.transcript.every(entry => entry.roundNumber === 2)).toBe(true);
    expect(state.transcript[0]?.toolResult?.content).toBe('late result');
  });
});

describe('chunk gluing per channel', () => {
  it('joins consecutive diagnostic chunks with the line breaks they lack', () => {
    const state = reduceEventBatch(initialCoreState(), [
      channelEvent(1, 'diagnostic', '[codex thread 01a0 started]'),
      channelEvent(2, 'diagnostic', '[codex turn started]'),
      channelEvent(3, 'diagnostic', '[codex turn complete: in=10 out=2]'),
    ]);

    expect(state.transcript).toHaveLength(1);
    expect(state.transcript[0]?.content).toBe(
      '[codex thread 01a0 started]\n[codex turn started]\n[codex turn complete: in=10 out=2]',
    );
  });

  it('does not double the separator when a chunk already ends a line', () => {
    const state = reduceEventBatch(initialCoreState(), [
      channelEvent(1, 'diagnostic', 'driver: agentshim\n'),
      channelEvent(2, 'diagnostic', '--- input ---'),
    ]);

    expect(state.transcript[0]?.content).toBe('driver: agentshim\n--- input ---');
  });

  it('still concatenates analysis chunks raw, because they are stream fragments', () => {
    const state = reduceEventBatch(initialCoreState(), [
      channelEvent(1, 'analysis', 'the ring buffer '),
      channelEvent(2, 'analysis', 'is the hot path'),
    ]);

    expect(state.transcript).toHaveLength(1);
    expect(state.transcript[0]?.content).toBe('the ring buffer is the hot path');
  });
});

describe('the carried-forward profile flag', () => {
  it('lands on the round whose round_finished event skipped profiling', () => {
    const state = reduceEvent(initialCoreState(), roundFinishedEvent(1, {profile_skipped: true}));

    expect(state.rounds).toHaveLength(1);
    expect(state.rounds[0]?.status).toBe('completed');
    expect(state.rounds[0]?.profileSkipped).toBe(true);
  });

  it('stays unset when the event lacks the field, as legacy streams do', () => {
    const state = reduceEvent(initialCoreState(), roundFinishedEvent(1, {}));

    expect(state.rounds[0]?.status).toBe('completed');
    expect(state.rounds[0]?.profileSkipped).toBeUndefined();
  });
});

/** One stream touching every transcript merge rule, plus both chat threads. */
function mixedTranscriptEvents(): RunEvent[] {
  return [
    outputEvent(1, 'hello '),
    outputEvent(2, 'world'),
    outputEvent(3, 'separate', 'turn-2'),
    toolEvent(4, 'tool_call', 'call-a', 'first'),
    toolEvent(5, 'tool_call', 'call-b', 'second'),
    toolEvent(6, 'tool_result', 'call-b', 'second result'),
    outputEvent(7, 'between', 'turn-3'),
    toolEvent(8, 'tool_result', 'call-a', 'first result'),
    chatAnswerEvent(9, 'default answer'),
    threadCreatedEvent(10, 'thread-x', 'anthropic'),
    chatAnswerEvent(11, 'thread answer', 'thread-x'),
    chatAnswerEvent(12, 'default again'),
    todoEvent(13, 'exec-1', 'Write the fold'),
    roundOutputEvent(14, 2),
    roundToolEvent(15, 'tool_call', 'call-c', 'third'),
    roundToolEvent(16, 'tool_result', 'call-c', 'third result'),
  ];
}

function roundOutputEvent(sequence: number, round: number): RunEvent {
  return {
    ...baseEvent(sequence, 'agent_output_chunk'),
    round_label: `round-${round}-implementer`,
    invocation_id: `turn-${sequence}`,
    data: {kind: 'agent_output_chunk', channel: 'assistant', content: `entry ${sequence}`},
  };
}

function roundToolEvent(
  sequence: number,
  kind: 'tool_call' | 'tool_result',
  callId: string,
  content: string,
): RunEvent {
  return {
    ...toolEvent(sequence, kind, callId, content),
    round_label: 'round-2-implementer',
  };
}

function roundFinishedEvent(sequence: number, extra: {profile_skipped?: boolean}): RunEvent {
  return {
    ...baseEvent(sequence, 'round_finished'),
    round_label: 'round-1',
    data: {
      kind: 'round_finished',
      attempts: 1,
      judge_verdict: 'pass',
      perf_metric: 900,
      perf_unit: 'ops/s',
      ...extra,
    },
  };
}

function baseEvent(sequence: number, type: RunEvent['type']): RunEvent {
  return {
    sequence,
    timestamp: `2026-01-01T00:00:0${sequence}Z`,
    type,
    agent_kind: 'implementer',
    round_label: 'round-1-implementer',
  };
}

function chatAnswerEvent(sequence: number, answer: string, threadId?: string): RunEvent {
  return {
    ...baseEvent(sequence, 'chat'),
    agent_kind: 'chat',
    round_label: 'experiment-chat',
    ...(threadId === undefined ? {} : {chat_thread_id: threadId}),
    data: {kind: 'chat', answer},
  };
}

/** One assistant-channel chunk of a chat turn, as the chat agent streams it. */
function chatStreamEvent(sequence: number, content: string, invocationId: string): RunEvent {
  return {
    ...outputEvent(sequence, content, invocationId),
    agent_kind: 'chat',
    round_label: 'experiment-chat',
  };
}

function threadCreatedEvent(sequence: number, threadId: string, provider: string): RunEvent {
  return {
    ...baseEvent(sequence, 'chat_thread_created'),
    agent_kind: 'chat',
    round_label: 'experiment-chat',
    chat_thread_id: threadId,
    data: {
      kind: 'chat_thread_created',
      thread_id: threadId,
      title: '',
      driver: 'agentshim',
      provider,
      model: 'opus',
      created_at: `2026-01-01T00:00:0${sequence}Z`,
    },
  };
}

function chatTitledEvent(sequence: number, threadId: string, title: string): RunEvent {
  return {
    ...baseEvent(sequence, 'chat'),
    agent_kind: 'chat',
    round_label: 'experiment-chat',
    chat_thread_id: threadId,
    data: {kind: 'chat', answer: 'answer', thread_title: title},
  };
}

/** One `agent_output_chunk` on a named channel, all within a single turn. */
function channelEvent(
  sequence: number,
  channel: 'analysis' | 'diagnostic',
  content: string,
): RunEvent {
  return {
    ...baseEvent(sequence, 'agent_output_chunk'),
    invocation_id: 'turn',
    data: {kind: 'agent_output_chunk', channel, content},
  };
}

function outputEvent(sequence: number, content: string, invocationId = 'turn'): RunEvent {
  return {
    ...baseEvent(sequence, 'agent_output_chunk'),
    invocation_id: invocationId,
    data: {kind: 'agent_output_chunk', channel: 'assistant', content},
  };
}

function executionEvent(
  sequence: number,
  type: RunEvent['type'],
  executionId: string,
  data: NonNullable<RunEvent['data']>,
): RunEvent {
  return {...baseEvent(sequence, type), execution_id: executionId, data};
}

function startedData(assignment: string): NonNullable<RunEvent['data']> {
  return {
    kind: 'agent_execution_started',
    stage: 'implementation',
    attempt: 1,
    system_prompt: '',
    user_prompt: assignment,
    activity: {
      kind: 'agent_execution_activity_changed',
      mode: 'thinking',
      summary: 'Starting',
      tool: null,
    },
  };
}

function checkpoint(
  executionId: string,
  overrides: Partial<NonNullable<RunSnapshot['active_executions']>[number]> = {},
): NonNullable<RunSnapshot['active_executions']>[number] {
  return {
    execution_id: executionId,
    agent_kind: 'judge',
    round_label: 'round-2-judge',
    stage: 'judging',
    attempt: 1,
    assignment: 'Review',
    started_at: '2026-01-01T00:00:00Z',
    activity: {
      kind: 'agent_execution_activity_changed',
      mode: 'thinking',
      summary: 'Reviewing',
      tool: null,
    },
    ...overrides,
  };
}

function toolEvent(
  sequence: number,
  kind: 'tool_call' | 'tool_result',
  callId: string,
  content: string,
): RunEvent {
  return {
    ...baseEvent(sequence, kind),
    invocation_id: 'turn',
    data:
      kind === 'tool_call'
        ? {kind, tool: 'Bash', call_id: callId, args: {command: content}}
        : {kind, tool: 'Bash', call_id: callId, content, is_error: false},
  };
}

function todoEvent(sequence: number, executionId: string, content: string): RunEvent {
  return {
    ...baseEvent(sequence, 'todo_update'),
    execution_id: executionId,
    data: {kind: 'todo_update', todos: [{content, status: 'in_progress'}]},
  };
}

function diagnosticEvent(
  sequence: number,
  type: RunEvent['type'],
  id: string,
  severity: 'warning' | 'error' | 'fatal',
  summary: string,
  options: {invocationId?: string; detail?: string | null} = {},
): RunEvent {
  return {
    ...baseEvent(sequence, type),
    ...(options.invocationId === undefined ? {} : {invocation_id: options.invocationId}),
    diagnostic: {
      id,
      code: 'agent_failed',
      summary,
      detail: options.detail ?? null,
      hint: null,
      scope: 'invocation',
      severity,
      retryability: 'manual',
      cause_id: null,
      debug_ref: null,
    },
  };
}
