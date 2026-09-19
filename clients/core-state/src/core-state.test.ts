import {describe, expect, it} from 'bun:test';
import {
  type ActiveAgentExecution,
  AgentOutputChannel,
  DiagnosticRetryability,
  DiagnosticScope,
  DiagnosticSeverity,
  EventStatus,
  EventType,
  ExecutionActivityMode,
  ExperimentsChangeReason,
  FrameworkSource,
  GateKind,
  RoundJudgeVerdict,
  type RunEvent,
  RunStatus,
} from '@vibesys/backend-client';
import {makeEvent, makeSnapshot, timestampOf} from '@vibesys/backend-client/testing';
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
import {executionStatusFor} from './execution-status.js';

describe('core state projection', () => {
  it('projects snapshots without changing event-derived history', () => {
    const prior = reduceEvent(initialCoreState(), outputEvent(4, 'kept'));
    const snapshot = makeSnapshot({
      runId: 'run',
      status: RunStatus.RUNNING,
      sequence: 9,
      agentKind: 'judge',
      roundLabel: 'round-2-judge',
      activeExecutions: [checkpoint('exec-1')],
    });

    const state = reduceSnapshot(prior, snapshot);

    expect(state.sequence).toBe(4);
    expect(state.transcript.map(entry => entry.content)).toEqual(['kept']);
    expect(state.agentKind).toBe('judge');
    expect(state.activeExecutions['exec-1']?.roundNumber).toBe(2);
  });

  it('rejects a snapshot older than the projected event cursor', () => {
    const current = reduceEvent(initialCoreState(), outputEvent(5, 'current'));
    const stale = makeSnapshot({
      runId: 'run',
      status: RunStatus.RUNNING,
      sequence: 4,
      agentKind: 'judge',
      roundLabel: 'round-2-judge',
      activeExecutions: [checkpoint('stale')],
    });

    expect(reduceSnapshot(current, stale)).toBe(current);
  });

  it('registers the chat threads a snapshot projects', () => {
    const state = reduceSnapshot(
      initialCoreState(),
      makeSnapshot({
        runId: 'run',
        status: RunStatus.RUNNING,
        sequence: 1,
        chatThreads: [
          {
            threadId: 'thread-a',
            title: 'Ring buffer sizing',
            driver: 'agentshim',
            provider: 'anthropic',
            model: 'opus',
          },
        ],
      }),
    );

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

    const state = reduceSnapshot(
      current,
      makeSnapshot({
        runId: 'run',
        status: RunStatus.RUNNING,
        sequence: 4,
        chatThreads: [
          {threadId: 'thread-a', title: '', driver: 'agentshim', provider: 'codex', model: 'gpt-5'},
        ],
      }),
    );

    expect(state.status).toBe(current.status);
    expect(state.chatThreads.map(thread => thread.id)).toEqual([
      DEFAULT_CHAT_THREAD_ID,
      'thread-a',
    ]);
  });

  it('leaves a stale snapshot that projects no chat threads identity-preserving', () => {
    const current = reduceEvent(initialCoreState(), outputEvent(5, 'current'));
    const stale = makeSnapshot({runId: 'run', status: RunStatus.RUNNING, sequence: 4});

    expect(reduceSnapshot(current, stale)).toBe(current);
  });

  it('merges projected chat threads with replayed ones without duplicating', () => {
    let current = reduceEvent(initialCoreState(), threadCreatedEvent(1, 'thread-a', 'anthropic'));
    current = reduceEvent(current, chatTitledEvent(2, 'thread-a', 'Replayed title'));

    const state = reduceSnapshot(
      current,
      makeSnapshot({
        runId: 'run',
        status: RunStatus.RUNNING,
        sequence: 3,
        chatThreads: [
          {
            threadId: 'thread-a',
            title: '',
            driver: 'agentshim',
            provider: 'anthropic',
            model: 'opus',
          },
          {
            threadId: 'thread-b',
            title: 'Projected',
            driver: 'agentshim',
            provider: 'codex',
            model: 'gpt-5',
          },
        ],
      }),
    );

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
      executionEvent(1, EventType.AGENT_EXECUTION_STARTED, 'exec', startedData('Implement')),
    );
    const rounds = started.rounds;
    const phases = started.phases;

    const streamed = reduceEvent(started, outputEvent(2, 'working', 'exec'));
    const diagnosed = reduceEvent(
      streamed,
      diagnosticEvent(3, EventType.AGENT_OUTPUT_CHUNK, 'warning', 'warning', 'Check output'),
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
    const started = executionEvent(1, EventType.AGENT_EXECUTION_STARTED, 'stale', {
      case: 'agentExecutionStarted',
      value: {
        stage: 'implementation',
        attempt: 1,
        systemPrompt: '',
        userPrompt: 'Implement the queue',
        activity: {mode: ExecutionActivityMode.THINKING, summary: 'Inspecting'},
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
      executionEvent(1, EventType.AGENT_EXECUTION_STARTED, 'first', startedData('First')),
    );
    state = reduceEvent(
      state,
      executionEvent(2, EventType.AGENT_EXECUTION_STARTED, 'second', startedData('Second')),
    );
    state = reduceEvent(
      state,
      executionEvent(3, EventType.AGENT_EXECUTION_ACTIVITY_CHANGED, 'second', {
        case: 'agentExecutionActivityChanged',
        value: {mode: ExecutionActivityMode.TOOL, summary: 'Running tests', tool: 'Bash'},
      }),
    );
    state = reduceEvent(
      state,
      executionEvent(4, EventType.AGENT_EXECUTION_FINISHED, 'first', {
        case: 'agentExecutionFinished',
        value: {},
      }),
    );

    expect(Object.keys(state.activeExecutions)).toEqual(['second']);
    expect(state.activeExecutions['second']?.activity).toEqual({
      mode: 'tool',
      summary: 'Running tests',
      tool: 'Bash',
    });
  });

  it('tracks structured output and tool status independently per execution', () => {
    let state = initialCoreState();
    state = reduceEvent(
      state,
      executionEvent(1, EventType.AGENT_EXECUTION_STARTED, 'first', startedData('First')),
    );
    state = reduceEvent(
      state,
      executionEvent(2, EventType.AGENT_EXECUTION_STARTED, 'second', startedData('Second')),
    );
    state = reduceEvent(
      state,
      statusEvent(3, 'first', 'agent_output_chunk', {
        progress: 'Round 1/3',
        agentLabel: 'Implementer',
        elapsedSeconds: 12.5,
        inputTokens: 8_000,
        contextWindow: 200_000,
      }),
    );
    state = reduceEvent(
      state,
      statusEvent(4, 'second', 'tool_call', {
        progress: 'Reviewing',
        agentLabel: 'Judge',
        elapsedSeconds: 3,
        inputTokens: 2_000,
        contextWindow: 100_000,
      }),
    );

    expect(state.executionStatuses).toEqual({
      first: {
        executionId: 'first',
        sequence: 3,
        observedAt: '2026-01-01T00:00:03.000Z',
        progress: 'Round 1/3',
        agentLabel: 'Implementer',
        elapsedSeconds: 12.5,
        inputTokens: 8_000,
        contextWindow: 200_000,
      },
      second: {
        executionId: 'second',
        sequence: 4,
        observedAt: '2026-01-01T00:00:04.000Z',
        progress: 'Reviewing',
        agentLabel: 'Judge',
        elapsedSeconds: 3,
        inputTokens: 2_000,
        contextWindow: 100_000,
      },
    });
    expect(state.usage).toEqual({inputTokens: 2_000, contextWindow: 100_000, model: null});

    state = reduceEvent(state, statusEvent(5, 'first', 'agent_output_chunk', {inputTokens: 9_000}));
    expect(state.executionStatuses['first']).toMatchObject({
      sequence: 5,
      inputTokens: 9_000,
      contextWindow: 200_000,
    });
    expect(state.usage).toEqual({inputTokens: 9_000, contextWindow: 200_000, model: null});
  });

  it('treats a zero input-token status as no update, keeping the live reading', () => {
    let state = reduceEvent(
      initialCoreState(),
      executionEvent(1, EventType.AGENT_EXECUTION_STARTED, 'first', startedData('First')),
    );
    state = reduceEvent(
      state,
      statusEvent(2, 'first', 'agent_output_chunk', {inputTokens: 8_000, contextWindow: 200_000}),
    );
    expect(state.usage).toEqual({inputTokens: 8_000, contextWindow: 200_000, model: null});

    // The backend seeds input_tokens at 0 and emits that before the agent's
    // next completion; a 0 must not overwrite the live count or blank the meter.
    state = reduceEvent(state, statusEvent(3, 'first', 'agent_output_chunk', {inputTokens: 0}));
    expect(state.executionStatuses['first']).toMatchObject({sequence: 3, inputTokens: 8_000});
    expect(state.usage).toEqual({inputTokens: 8_000, contextWindow: 200_000, model: null});
  });

  it('reconciles status through checkpoints and clears only the execution that finishes', () => {
    let state = reduceEvent(initialCoreState(), statusEvent(1, 'first', 'agent_output_chunk'));
    state = reduceEvent(state, statusEvent(2, 'second', 'tool_call'));
    state = reconcileActiveExecutions(state, [checkpoint('first'), checkpoint('second')], 2);

    expect(Object.keys(state.executionStatuses)).toEqual(['first', 'second']);

    state = reduceEvent(
      state,
      executionEvent(3, EventType.AGENT_EXECUTION_FINISHED, 'first', {
        case: 'agentExecutionFinished',
        value: {},
      }),
    );
    expect(Object.keys(state.executionStatuses)).toEqual(['second']);

    state = reconcileActiveExecutions(
      state,
      [checkpoint('second', {startedAt: timestampOf('2026-01-01T00:00:05Z')})],
      3,
    );
    const restarted = state.activeExecutions['second'];
    if (restarted === undefined) throw new Error('checkpoint dropped its active execution');
    expect(executionStatusFor(state.executionStatuses, restarted)).toBeUndefined();

    state = reduceEvent(state, outputEvent(4, 'legacy after checkpoint', 'second'));
    expect(state.executionStatuses).toEqual({});
  });

  it('leaves legacy output without structured status unchanged', () => {
    const started = reduceEvent(
      initialCoreState(),
      executionEvent(1, EventType.AGENT_EXECUTION_STARTED, 'first', startedData('First')),
    );
    const projected = reduceEvent(started, outputEvent(2, 'legacy', 'first'));

    expect(projected.executionStatuses).toBe(started.executionStatuses);
    expect(projected.usage).toBeNull();
  });

  it('advances repeated unsequenced status without replacing sequenced status', () => {
    let state = reduceEvent(initialCoreState(), statusEvent(0, 'first', 'agent_output_chunk'));
    state = reduceEvent(
      state,
      statusEvent(0, 'first', 'tool_call', {progress: 'second unsequenced'}),
    );
    expect(state.executionStatuses['first']?.progress).toBe('second unsequenced');

    state = reduceEvent(
      state,
      statusEvent(2, 'first', 'agent_output_chunk', {
        progress: 'sequenced',
        inputTokens: 7_000,
        contextWindow: 20_000,
      }),
    );
    state = reduceEvent(
      state,
      statusEvent(0, 'first', 'agent_output_chunk', {
        progress: 'stale unsequenced',
        inputTokens: 1,
        contextWindow: 2,
      }),
    );
    expect(state.executionStatuses['first']?.progress).toBe('sequenced');
    expect(state.executionStatuses['first']?.sequence).toBe(2);
    expect(state.usage).toEqual({inputTokens: 7_000, contextWindow: 20_000, model: null});
  });

  it('clears pending status when the run terminates before a checkpoint arrives', () => {
    const pending = reduceEvent(initialCoreState(), statusEvent(1, 'first', 'agent_output_chunk'));
    const ended = reduceEvent(
      pending,
      baseEvent(2, EventType.RUN_FAILED, {agentKind: undefined, roundLabel: undefined}),
    );

    expect(ended.executionStatuses).toEqual({});
  });

  it('captures runtime identity from agent_execution_started when present', () => {
    const state = reduceEvent(
      initialCoreState(),
      executionEvent(
        1,
        EventType.AGENT_EXECUTION_STARTED,
        'first',
        startedData('Implement the queue', {
          driver: 'agentshim',
          provider: 'codex',
          model: 'gpt-5.1-codex-max',
        }),
      ),
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
      executionEvent(
        1,
        EventType.AGENT_EXECUTION_STARTED,
        'first',
        startedData('Implement the queue'),
      ),
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
    const snapshot = makeSnapshot({
      runId: 'run',
      status: RunStatus.RUNNING,
      sequence: 1,
      agentKind: 'judge',
      roundLabel: 'round-2-judge',
      activeExecutions: [
        checkpoint('exec-1', {driver: 'agentshim', provider: 'codex', model: 'gpt-5.1-codex-max'}),
      ],
    });

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
    let state = reduceEvent(
      initialCoreState(),
      baseEvent(1, EventType.TOOL_CALL, {
        executionId: 'turn',
        data: {
          case: 'toolCall',
          value: {tool: 'Edit', callId: 'call-long', args: arguments_},
        },
      }),
    );
    state = reduceEvent(
      state,
      baseEvent(2, EventType.TOOL_RESULT, {
        executionId: 'turn',
        data: {
          case: 'toolResult',
          value: {
            tool: 'Edit',
            callId: 'call-long',
            content: 'result '.repeat(40),
            isError: true,
          },
        },
      }),
    );

    expect(state.transcript[0]?.toolArguments).toEqual(arguments_);
    expect(state.transcript[0]?.toolResult).toMatchObject({
      tool: 'Edit',
      callId: 'call-long',
      content: 'result '.repeat(40),
      isError: true,
    });
    expect(state.transcript[0]?.toolCall).toBeUndefined();
    expect(state.transcript[0]?.toolResponse).toBeUndefined();
  });

  it('carries the typed result payload onto the merged transcript entry', () => {
    let state = reduceEvent(
      initialCoreState(),
      baseEvent(1, EventType.TOOL_CALL, {
        executionId: 'turn',
        data: {case: 'toolCall', value: {tool: 'shell', callId: 'call-1', args: {cmd: 'ls'}}},
      }),
    );
    state = reduceEvent(
      state,
      baseEvent(2, EventType.TOOL_RESULT, {
        executionId: 'turn',
        data: {
          case: 'toolResult',
          value: {
            tool: 'shell',
            callId: 'call-1',
            content: 'file.txt',
            payload: {
              case: 'command',
              value: {stdout: 'file.txt', stderr: '', exitCode: 0, duration: 0.1},
            },
          },
        },
      }),
    );

    expect(state.transcript).toHaveLength(1);
    expect(state.transcript[0]?.toolResult?.payload).toMatchObject({
      case: 'command',
      value: {stdout: 'file.txt', stderr: '', exitCode: 0, duration: 0.1},
    });
  });

  it('keeps chat-agent events out of the experiment transcript', () => {
    const chat = {
      ...outputEvent(1, 'answer'),
      agentKind: 'chat',
      roundLabel: 'experiment-chat',
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

  it('folds a stamped answer over the turn that streamed under the same invocation', () => {
    let state = initialCoreState();
    state = reduceEvent(state, chatStreamEvent(1, 'The queue ', 'exec-1'));
    state = reduceEvent(state, chatStreamEvent(2, 'is lock-free.', 'exec-1'));
    state = reduceEvent(state, chatAnswerEvent(3, 'The queue is lock-free.', undefined, 'exec-1'));

    expect(state.chatTranscript).toHaveLength(1);
    expect(state.chatTranscript[0]).toMatchObject({id: '1', content: 'The queue is lock-free.'});
    expect(state.chatTranscript[0]?.turnId).toBeUndefined();
  });

  it('never folds an answer over a turn another invocation abandoned', () => {
    const events = [
      chatStreamEvent(1, 'partial ', 'exec-1'),
      chatStreamEvent(2, 'answer', 'exec-1'),
      // exec-1 failed before recording a terminal answer; the next question's
      // answer arrives stamped with its own invocation.
      chatAnswerEvent(3, 'later answer', undefined, 'exec-2'),
    ];
    const batched = reduceEventBatch(initialCoreState(), events);
    let single = initialCoreState();
    for (const item of events) single = reduceEvent(single, item);

    // The abandoned turn keeps its partial stream; the unrelated answer
    // appends instead of rewriting it in place.
    for (const state of [batched, single]) {
      expect(state.chatTranscript.map(entry => [entry.id, entry.content])).toEqual([
        ['1', 'partial answer'],
        ['3', 'later answer'],
      ]);
      expect(state.chatTranscript[0]?.turnId).toBe('exec-1');
    }
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
    state = reduceEvent(
      state,
      chatAnswerEvent(2, 'first answer', 'thread-a', undefined, 'why did r2 regress'),
    );

    expect(state.chatThreads.find(thread => thread.id === 'thread-a')?.title).toBe(
      'why did r2 regress',
    );
  });

  it('names a thread from a titled turn even when its creation replayed away', () => {
    const state = reduceEvent(
      initialCoreState(),
      chatAnswerEvent(1, 'answer', 'thread-x', undefined, 'orphan thread'),
    );

    expect(state.chatThreads.find(thread => thread.id === 'thread-x')?.title).toBe('orphan thread');
    expect(state.chatTranscripts['thread-x']?.map(entry => entry.content)).toEqual(['answer']);
  });

  it('drops legacy chat tool chunks per thread once typed events appear', () => {
    let state = initialCoreState();
    state = reduceEvent(
      state,
      baseEvent(1, EventType.TOOL_CALL, {
        agentKind: 'chat',
        chatThreadId: 'thread-a',
        data: {case: 'toolCall', value: {tool: 'read_file', args: {}}},
      }),
    );
    // The default thread saw no typed events, so its legacy chunks survive.
    state = reduceEvent(state, {
      ...outputEvent(2, 'legacy default output'),
      agentKind: 'chat',
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
    const state = reduceEvent(
      initialCoreState(),
      baseEvent(8, EventType.BENCHMARK_RESULT, {
        data: {case: 'benchmarkResult', value: {metric: 'ops', value: 42, unit: 'ops/s'}},
      }),
    );

    expect(state.benchmarks).toEqual([
      {sequence: 8, roundNumber: 1, metric: 'ops', value: 42, unit: 'ops/s'},
    ]);
  });

  it('records structured diagnostics as durable facts', () => {
    const state = reduceEvent(
      initialCoreState(),
      baseEvent(3, EventType.RUN_FAILED, {
        diagnostic: {
          id: 'diag-1',
          code: 'agent_failed',
          summary: 'Agent failed.',
          detail: 'Exit 2',
          hint: 'Retry.',
          scope: DiagnosticScope.RUN,
          severity: DiagnosticSeverity.FATAL,
          retryability: DiagnosticRetryability.MANUAL,
        },
      }),
    );

    expect(hasRunEnded(state)).toBe(true);
    expect(state.diagnostics).toMatchObject([
      {id: 'diag-1', summary: 'Agent failed.', severity: 'fatal', sequence: 3},
    ]);
  });

  it('prefers a structured diagnostic over a conflicting legacy failure envelope', () => {
    const state = reduceEvent(
      initialCoreState(),
      baseEvent(3, EventType.CONFIGURATION_FAILED, {
        data: {
          case: 'configurationFailed',
          value: {
            code: 'legacy_code',
            message: 'Legacy summary',
            stage: 'configuration',
            exitCode: 2,
          },
        },
        diagnostic: {
          id: 'diag-structured',
          code: 'structured_code',
          summary: 'Structured summary',
          detail: 'Structured detail',
          scope: DiagnosticScope.RUN,
          severity: DiagnosticSeverity.ERROR,
          retryability: DiagnosticRetryability.MANUAL,
        },
      }),
    );

    expect(state.diagnostics).toMatchObject([
      {
        id: 'diag-structured',
        code: 'structured_code',
        summary: 'Structured summary',
        detail: 'Structured detail',
        scope: 'run',
        severity: 'error',
      },
    ]);
  });

  it('classifies legacy invocation and run failure envelopes by scope', () => {
    const invocation = reduceEvent(
      initialCoreState(),
      baseEvent(4, EventType.INVOCATION_FINISHED, {
        status: EventStatus.FAILED,
        data: {case: 'invocationFinished', value: {}},
      }),
    );
    const failed = reduceEvent(
      initialCoreState(),
      baseEvent(5, EventType.RUN_FAILED, {text: 'worker exited'}),
    );
    const interrupted = reduceEvent(
      initialCoreState(),
      baseEvent(6, EventType.RUN_INTERRUPTED, {
        data: {
          case: 'runInterrupted',
          value: {reason: 'launcher_terminated', signal: 'SIGTERM'},
        },
      }),
    );

    expect(invocation.diagnostics).toMatchObject([
      {scope: 'invocation', severity: 'error', summary: 'Agent invocation failed.'},
    ]);
    expect(failed.diagnostics).toMatchObject([
      {scope: 'run', failureKind: 'run', severity: 'fatal', summary: 'worker exited'},
    ]);
    expect(interrupted.diagnostics).toMatchObject([
      {
        scope: 'run',
        failureKind: 'run_interruption',
        severity: 'fatal',
        summary: 'launcher_terminated (SIGTERM)',
      },
    ]);
  });

  it('promotes a repeated diagnostic id with richer terminal detail', () => {
    const initialFailure = reduceEvent(
      initialCoreState(),
      diagnosticEvent(1, EventType.INVOCATION_FINISHED, 'diag-1', 'error', 'Agent failed.', {
        invocationId: 'invocation-1',
      }),
    );
    const state = reduceEvent(
      initialFailure,
      diagnosticEvent(2, EventType.RUN_FAILED, 'diag-1', 'fatal', 'Agent failed terminally.', {
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
      diagnosticEvent(1, EventType.INVOCATION_FINISHED, 'diag-1', 'error', 'First failure.', {
        invocationId: 'invocation-1',
      }),
    );
    state = reduceEvent(
      state,
      diagnosticEvent(2, EventType.PHASE_FINISHED, 'diag-2', 'error', 'Second failure.', {
        invocationId: 'invocation-1',
      }),
    );

    expect(state.diagnostics.map(diagnostic => diagnostic.id)).toEqual(['diag-1', 'diag-2']);
  });

  it('retains structured configuration failure detail in the transcript', () => {
    const state = reduceEvent(
      initialCoreState(),
      baseEvent(3, EventType.CONFIGURATION_FAILED, {
        data: {
          case: 'configurationFailed',
          value: {
            code: 'resume_limit_exhausted',
            message: 'This run has completed 30 rounds.',
            usage: 'Use a larger limit.',
            stage: 'configuration',
            exitCode: 2,
          },
        },
      }),
    );

    expect(state.transcript[0]?.content).toContain('resume_limit_exhausted');
    expect(state.transcript[0]?.content).toContain('Use a larger limit.');
    expect(state.diagnostics[0]).toMatchObject({
      failureKind: 'configuration',
      summary:
        'This run has completed 30 rounds.\n\nUse a larger limit.\n\nCode: resume_limit_exhausted · Stage: configuration',
    });
  });

  it('projects an interruption discriminator without presentation labels', () => {
    const state = reduceEvent(
      initialCoreState(),
      baseEvent(3, EventType.RUN_INTERRUPTED, {
        data: {
          case: 'runInterrupted',
          value: {reason: 'launcher_terminated', signal: 'SIGTERM'},
        },
      }),
    );

    expect(state.diagnostics[0]).toMatchObject({
      failureKind: 'run_interruption',
      scope: 'run',
      summary: 'launcher_terminated (SIGTERM)',
      severity: 'fatal',
    });
    expect('title' in (state.diagnostics[0] ?? {})).toBe(false);
  });

  it('distinguishes failed and interrupted terminal transcript entries', () => {
    const failed = reduceEvent(initialCoreState(), baseEvent(1, EventType.RUN_FAILED, {text: ''}));
    const interrupted = reduceEvent(
      initialCoreState(),
      baseEvent(1, EventType.RUN_INTERRUPTED, {
        text: '',
        data: {
          case: 'runInterrupted',
          value: {reason: 'Operator stopped the run', signal: 'SIGINT'},
        },
      }),
    );

    expect(failed.transcript.at(-1)).toMatchObject({
      content: 'Run failed.',
      label: 'Run failed',
    });
    expect(interrupted.transcript.at(-1)).toMatchObject({
      content: 'Operator stopped the run (SIGINT)',
      label: 'Run interrupted',
    });
  });

  it('keeps typed payload precedence over conflicting event-type fallbacks', () => {
    const chat = reduceEvent(
      initialCoreState(),
      baseEvent(1, EventType.PHASE_STARTED, {
        data: {case: 'chat', value: {answer: 'typed answer'}},
      }),
    );
    const gate = reduceEvent(
      initialCoreState(),
      baseEvent(2, EventType.RUN_FAILED, {
        data: {
          case: 'gateStarted',
          value: {gate: GateKind.VALIDATION, recipe: 'focused-tests', command: 'bun test'},
        },
      }),
    );

    expect(chat.transcript).toMatchObject([{kind: 'assistant', content: 'typed answer'}]);
    expect(gate.transcript).toMatchObject([
      {
        kind: 'status',
        content: 'running focused-tests',
        label: 'framework-validation · round-1-implementer',
      },
    ]);
  });

  it('exposes experiment changes only as stream-derived invalidation', () => {
    const state = reduceEvent(
      initialCoreState(),
      baseEvent(12, EventType.EXPERIMENTS_CHANGED, {
        data: {
          case: 'experimentsChanged',
          value: {reason: ExperimentsChangeReason.ROUND_PERSISTED},
        },
      }),
    );

    expect(state.experimentsRevision).toBe(12);
    expect('experimentLog' in state).toBe(false);
  });
});

describe('whether a run has ended', () => {
  const withStatus = (status: CoreRunStatus): CoreState => ({...initialCoreState(), status});

  it('classifies every run status the projection can hold', () => {
    expect(hasRunEnded(withStatus('completed'))).toBe(true);
    expect(hasRunEnded(withStatus('failed'))).toBe(true);
    expect(hasRunEnded(withStatus('stopped'))).toBe(true);
    expect(hasRunEnded(withStatus('connecting'))).toBe(false);
    expect(hasRunEnded(withStatus('starting'))).toBe(false);
    expect(hasRunEnded(withStatus('running'))).toBe(false);
    expect(hasRunEnded(withStatus('pausing'))).toBe(false);
    expect(hasRunEnded(withStatus('paused'))).toBe(false);
    expect(hasRunEnded(withStatus('stopping'))).toBe(false);
  });

  it('reads an ended run from a bootstrapped snapshot', () => {
    const snapshot = makeSnapshot({runId: 'run', status: RunStatus.COMPLETED, sequence: 4});

    const state = reduceSnapshot(initialCoreState(), snapshot);

    expect(hasRunEnded(state)).toBe(true);
  });

  // A resumed run replays the previous process's failure ahead of its own
  // start. Whether the run has ended is derived from the status, so the later
  // `run_started` cannot leave the projection looking finished while the run
  // is live.
  it('has not ended after a resumed run replays a failure then a start', () => {
    const state = reduceEventBatch(initialCoreState(), [
      baseEvent(1, EventType.RUN_FAILED),
      runStartedEvent(2),
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
    baseEvent(sequence, EventType.RUN_STATUS_CHANGED, {
      data: {
        case: 'runStatusChanged',
        value: {status: wireRunStatus(status), previous: wireRunStatus(previous)},
      },
    });

  it('folds a pause request, its boundary, and the resume', () => {
    const requested = reduceEvent(initialCoreState(), statusEvent(1, 'pausing', 'running'));
    expect(requested.status).toBe('pausing');

    const paused = reduceEvent(requested, statusEvent(2, 'paused', 'pausing'));
    expect(paused.status).toBe('paused');

    const resumed = reduceEvent(paused, statusEvent(3, 'running', 'paused'));
    expect(resumed.status).toBe('running');
    expect(hasRunEnded(resumed)).toBe(false);
  });

  it('folds a stop request as live until its boundary ends the run', () => {
    const requested = reduceEvent(initialCoreState(), statusEvent(1, 'stopping', 'running'));
    expect(requested.status).toBe('stopping');
    expect(hasRunEnded(requested)).toBe(false);

    const stopped = reduceEvent(requested, statusEvent(2, 'stopped', 'stopping'));
    expect(stopped.status).toBe('stopped');
    expect(hasRunEnded(stopped)).toBe(true);
  });

  it('keeps a run live when a resume cancels the pending stop', () => {
    const state = reduceEventBatch(initialCoreState(), [
      statusEvent(1, 'stopping', 'running'),
      statusEvent(2, 'running', 'stopping'),
    ]);

    expect(state.status).toBe('running');
    expect(hasRunEnded(state)).toBe(false);
  });

  it('drops the active executions of an operator-stopped run', () => {
    const running = reduceSnapshot(
      initialCoreState(),
      makeSnapshot({
        runId: 'run',
        status: RunStatus.STOPPING,
        sequence: 1,
        activeExecutions: [checkpoint('exec-1')],
      }),
    );

    const state = reduceEvent(running, statusEvent(2, 'stopped', 'stopping'));

    expect(state.activeExecutions).toEqual({});
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
    const running = reduceSnapshot(
      initialCoreState(),
      makeSnapshot({
        runId: 'run',
        status: RunStatus.PAUSED,
        sequence: 1,
        activeExecutions: [checkpoint('exec-1')],
      }),
    );

    const state = reduceEvent(running, statusEvent(2, 'failed', 'paused'));

    expect(state.activeExecutions).toEqual({});
  });

  it('keeps an ended run ended against a snapshot no newer than the fold', () => {
    const ended = reduceEventBatch(initialCoreState(), [
      statusEvent(1, 'pausing', 'running'),
      statusEvent(2, 'completed', 'pausing'),
      baseEvent(3, EventType.RUN_FINISHED),
    ]);

    const stale = reduceSnapshot(
      ended,
      makeSnapshot({
        runId: 'run',
        status: RunStatus.RUNNING,
        sequence: 3,
      }),
    );

    expect(stale.status).toBe('completed');
  });

  it('reads a resumed run as running from the transition after the replay', () => {
    const state = reduceEventBatch(initialCoreState(), [
      statusEvent(1, 'completed', 'running'),
      baseEvent(2, EventType.RUN_FINISHED),
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
  const runLog: RunEvent[] = [runStartedEvent(1), outputEvent(2, 'two'), outputEvent(3, 'three')];

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
    const superseded = reduceSnapshot(
      initialCoreState(),
      makeSnapshot({
        runId: 'run',
        status: RunStatus.RUNNING,
        sequence: 1,
        chatThreads: [
          {
            threadId: 'thread-a',
            title: 'Ring buffer sizing',
            driver: 'agentshim',
            provider: 'anthropic',
            model: 'opus',
          },
        ],
      }),
    );

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

  it('keeps batch and sequential folds equivalent across randomized event families', () => {
    const events = randomizedFoldEvents();

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
      baseEvent(3, EventType.TOOL_RESULT, {
        executionId: 'turn',
        data: {
          case: 'toolResult',
          value: {tool: 'Bash', content: 'anonymous result', isError: false},
        },
      }),
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

describe('the framework-validation gate command adapter', () => {
  // Legacy adapter for the shape recorded at
  // clients/tui/dev/fixtures/bad-cpp-round1.jsonl:388, produced by loop.py's
  // `ctx.lprint(f"[framework-validation] running {recipe.name}: {recipe.command}")`
  // on the diagnostic channel. See #692 / PR #697.
  const GATE_LINE =
    '[framework-validation] running build-and-correctness-gate: mkdir -p .cache/tmp && TMPDIR="$PWD/.cache/tmp" make -s all && ./bin/tests\n';

  it('splits the recorded gate line into prose and a command field', () => {
    const state = reduceEvent(initialCoreState(), channelEvent(1, 'diagnostic', GATE_LINE));

    expect(state.transcript[0]?.content).toBe(
      '[framework-validation] running build-and-correctness-gate: ',
    );
    expect(state.transcript[0]?.command).toBe(
      'mkdir -p .cache/tmp && TMPDIR="$PWD/.cache/tmp" make -s all && ./bin/tests',
    );
  });

  it('leaves prose with an ordinary colon alone', () => {
    const state = reduceEvent(initialCoreState(), channelEvent(1, 'diagnostic', 'Ratio: 3 to 1'));

    expect(state.transcript[0]?.content).toBe('Ratio: 3 to 1');
    expect(state.transcript[0]?.command).toBeUndefined();
  });

  it('leaves untagged prose that mentions "running" alone', () => {
    const state = reduceEvent(
      initialCoreState(),
      channelEvent(1, 'diagnostic', 'Currently running the correctness gate: watch for output'),
    );

    expect(state.transcript[0]?.command).toBeUndefined();
  });

  it('leaves prose that says "running" under a different source tag alone', () => {
    // The real neighboring line, bad-cpp-round1.jsonl:393: the same
    // "running: <command>" shape, a different tag. Only the exact
    // "[framework-validation] running <recipe>: " prefix qualifies.
    const state = reduceEvent(
      initialCoreState(),
      channelEvent(1, 'diagnostic', '[framework-benchmark] running: ./bin/bench --scale 1.0\n'),
    );

    expect(state.transcript[0]?.command).toBeUndefined();
  });

  it('leaves a bracket tag with no "running" command alone', () => {
    const state = reduceEvent(
      initialCoreState(),
      channelEvent(1, 'diagnostic', '[framework-validation] PASS\n'),
    );

    expect(state.transcript[0]?.content).toBe('[framework-validation] PASS\n');
    expect(state.transcript[0]?.command).toBeUndefined();
  });

  it('captures a command that spans multiple lines', () => {
    const state = reduceEvent(
      initialCoreState(),
      channelEvent(1, 'diagnostic', '[framework-validation] running gate: line1\nline2\n'),
    );

    expect(state.transcript[0]?.command).toBe('line1\nline2');
  });

  it('does not set an empty command after the colon', () => {
    const state = reduceEvent(
      initialCoreState(),
      channelEvent(1, 'diagnostic', '[framework-validation] running gate: \n'),
    );

    expect(state.transcript[0]?.command).toBeUndefined();
    expect(state.transcript[0]?.content).toBe('[framework-validation] running gate: \n');
  });
});

describe('the carried-forward profile flag', () => {
  it('lands on the round whose round_finished event skipped profiling', () => {
    const state = reduceEvent(initialCoreState(), roundFinishedEvent(1, {profileSkipped: true}));

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

describe('typed framework events', () => {
  it('renders a gate start as the gate running, with its command in a separate field', () => {
    const state = reduceEvent(
      initialCoreState(),
      frameworkEvent(
        1,
        EventType.GATE_STARTED,
        {
          case: 'gateStarted',
          value: {gate: GateKind.VALIDATION, recipe: 'focused-tests', command: 'uv run pytest -q'},
        },
        {status: EventStatus.ACTIVE},
      ),
    );

    expect(state.transcript).toMatchObject([
      {
        kind: 'status',
        content: 'running focused-tests',
        command: 'uv run pytest -q',
        label: 'framework-validation · round-1',
        roundLabel: 'round-1',
        roundNumber: 1,
      },
    ]);
    // The command lives only in the `command` field: prose must not repeat it.
    expect(state.transcript[0]?.content).not.toContain('uv run pytest -q');
  });

  it('leaves `command` unset on a gate start with no command', () => {
    const state = reduceEvent(
      initialCoreState(),
      frameworkEvent(
        1,
        EventType.GATE_STARTED,
        {case: 'gateStarted', value: {gate: GateKind.ACCURACY}},
        {status: EventStatus.ACTIVE},
      ),
    );

    expect(state.transcript[0]?.content).toBe('running');
    expect(state.transcript[0]?.command).toBeUndefined();
  });

  // The framework speaks for itself even while an agent phase is active, and
  // even if an envelope arrived with agent context stamped on it: routing
  // these labels through `labelFor` would render this one as `judge · round-1`.
  it('never attributes a framework event to the active agent', () => {
    let state = reduceEvent(
      initialCoreState(),
      executionEvent(
        1,
        EventType.AGENT_EXECUTION_STARTED,
        'exec-judge',
        startedData('Review the diff'),
      ),
    );
    state = reduceEvent(state, {
      ...frameworkEvent(
        2,
        EventType.GATE_STARTED,
        {case: 'gateStarted', value: {gate: GateKind.VALIDATION, recipe: 'focused-tests'}},
        {status: EventStatus.ACTIVE},
      ),
      agentKind: 'judge',
    });

    const entry = state.transcript.at(-1);
    expect(entry?.label).toBe('framework-validation · round-1');
    expect(entry?.agentKind).toBeUndefined();
  });

  it('renders gate outcomes: pass, reused pass, and bare pass', () => {
    const pass = reduceEvent(
      initialCoreState(),
      frameworkEvent(
        1,
        EventType.GATE_FINISHED,
        {case: 'gateFinished', value: {gate: GateKind.VALIDATION, recipe: 'focused-tests'}},
        {status: EventStatus.COMPLETED},
      ),
    );
    const reused = reduceEvent(
      initialCoreState(),
      frameworkEvent(
        1,
        EventType.GATE_FINISHED,
        {case: 'gateFinished', value: {gate: GateKind.VALIDATION, recipe: 'lint', reused: true}},
        {status: EventStatus.COMPLETED},
      ),
    );
    const bare = reduceEvent(
      initialCoreState(),
      frameworkEvent(
        1,
        EventType.GATE_FINISHED,
        {case: 'gateFinished', value: {gate: GateKind.ACCURACY}},
        {status: EventStatus.COMPLETED},
      ),
    );

    expect(pass.transcript[0]).toMatchObject({
      kind: 'status',
      content: 'PASS: focused-tests',
      label: 'framework-validation · round-1',
      tone: 'success',
    });
    expect(reused.transcript[0]).toMatchObject({content: 'reused PASS: lint', tone: 'success'});
    expect(bare.transcript[0]).toMatchObject({
      content: 'PASS',
      label: 'framework-accuracy · round-1',
    });
  });

  it('renders a failed gate as a diagnostic card carrying the output tail', () => {
    const state = reduceEvent(
      initialCoreState(),
      frameworkEvent(
        1,
        EventType.GATE_FINISHED,
        {
          case: 'gateFinished',
          value: {gate: GateKind.ACCURACY, outputTail: 'assert 3 == 4\n1 failed'},
        },
        {status: EventStatus.FAILED},
      ),
    );

    expect(state.transcript).toMatchObject([
      {
        kind: 'diagnostic',
        content: 'FAIL\nassert 3 == 4\n1 failed',
        label: 'framework-accuracy · round-1',
        tone: 'failure',
      },
    ]);
  });

  it('renders a completed benchmark gate as the Benchmark card and folds the measurement', () => {
    const state = reduceEvent(
      initialCoreState(),
      frameworkEvent(
        7,
        EventType.GATE_FINISHED,
        {
          case: 'gateFinished',
          value: {gate: GateKind.BENCHMARK, metric: 'tok_per_sec', value: 42.5, unit: 'tok/s'},
        },
        {status: EventStatus.COMPLETED},
      ),
    );

    expect(state.transcript).toMatchObject([
      {kind: 'result', content: 'tok_per_sec: 42.5 tok/s', label: 'Benchmark', tone: 'success'},
    ]);
    expect(state.benchmarks).toEqual([
      {sequence: 7, roundNumber: 1, metric: 'tok_per_sec', value: 42.5, unit: 'tok/s'},
    ]);
  });

  it('describes each workspace snapshot aspect', () => {
    const aspects: {
      label?: string;
      commit?: string;
      baseline?: string;
      excludedPaths?: string[];
    }[] = [
      {label: 'round-1-implementer', commit: '3e7d0a1b2c4d5e6f708192a3b4c5d6e7f8091a2b'},
      {label: 'round-2-implementer'},
      {baseline: '9f2c1d4e6a7b8091a2b3c4d5e6f7a8b9c0d1e2f3'},
      {excludedPaths: ['logs/', 'artifacts/']},
    ];
    const contents = aspects.map(aspect => {
      const state = reduceEvent(
        initialCoreState(),
        frameworkEvent(
          1,
          EventType.WORKSPACE_SNAPSHOT,
          {case: 'workspaceSnapshot', value: {source: FrameworkSource.GIT_TRACKING, ...aspect}},
          {roundLabel: undefined},
        ),
      );
      return state.transcript[0];
    });

    expect(contents).toMatchObject([
      {kind: 'status', content: "snapshot 'round-1-implementer' at 3e7d0a1", label: 'git-tracking'},
      {content: "no changes to commit for 'round-2-implementer'", label: 'git-tracking'},
      {content: 'trusted input baseline: 9f2c1d4', label: 'git-tracking'},
      {content: 'excluded 2 paths from snapshots', label: 'git-tracking'},
    ]);
  });

  it('summarizes the run configuration once', () => {
    const state = reduceEvent(
      initialCoreState(),
      frameworkEvent(
        1,
        EventType.RUN_CONFIGURED,
        {
          case: 'runConfigured',
          value: {
            runLogPath: 'logs/run',
            projectRoot: '/work/project',
            model: 'claude-sonnet-4-5',
            objective: 'Raise decode throughput',
            searchPolicy: 'beam',
            source: FrameworkSource.LOOP,
          },
        },
        {roundLabel: undefined},
      ),
    );

    expect(state.transcript).toMatchObject([
      {
        kind: 'status',
        content:
          'objective: Raise decode throughput\nmodel: claude-sonnet-4-5\nsearch policy: beam',
        label: 'framework',
      },
    ]);
  });

  it('folds a framework warning into diagnostics, not the transcript', () => {
    const state = reduceEvent(initialCoreState(), frameworkWarningEvent(3));

    expect(state.transcript).toEqual([]);
    expect(state.diagnostics).toMatchObject([
      {id: 'warn-1', summary: 'profiler failed', severity: 'warning', source: 'loop', sequence: 3},
    ]);
  });

  it('projects a framework batch with no entry from the warning', () => {
    const state = reduceEventBatch(initialCoreState(), [
      frameworkEvent(
        1,
        EventType.RUN_CONFIGURED,
        {
          case: 'runConfigured',
          value: {runLogPath: 'logs/run', projectRoot: '/work', source: FrameworkSource.LOOP},
        },
        {roundLabel: undefined},
      ),
      frameworkEvent(
        2,
        EventType.GATE_STARTED,
        {case: 'gateStarted', value: {gate: GateKind.BENCHMARK, command: 'uv run python bench.py'}},
        {status: EventStatus.ACTIVE},
      ),
      frameworkEvent(
        3,
        EventType.GATE_FINISHED,
        {
          case: 'gateFinished',
          value: {gate: GateKind.BENCHMARK, metric: 'ops', value: 9, unit: 'ops/s'},
        },
        {status: EventStatus.COMPLETED},
      ),
      frameworkWarningEvent(4),
      frameworkEvent(5, EventType.WORKSPACE_SNAPSHOT, {
        case: 'workspaceSnapshot',
        value: {label: 'round-1', commit: 'abcdef0123456789', source: FrameworkSource.GIT_TRACKING},
      }),
    ]);

    expect(state.transcript).toHaveLength(4);
    expect(state.transcript.every(entry => entry.content.length > 0)).toBe(true);
    expect(state.transcript.every(entry => entry.agentKind === undefined)).toBe(true);
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

function randomizedFoldEvents(): RunEvent[] {
  let seed = 0x5eed;
  const nextRandom = (): number => {
    seed = (seed * 1_664_525 + 1_013_904_223) >>> 0;
    return seed;
  };
  const events: RunEvent[] = [];
  for (let sequence = 1; sequence <= 96; sequence += 1) {
    events.push(randomizedFoldEvent(sequence, nextRandom() % 8));
  }
  return events;
}

function randomizedFoldEvent(sequence: number, choice: number): RunEvent {
  if (choice === 0) return outputEvent(sequence, `assistant-${sequence}`, `turn-${sequence % 3}`);
  if (choice === 1) return channelEvent(sequence, 'diagnostic', `diagnostic-${sequence}`);
  if (choice === 2) return toolEvent(sequence, 'tool_call', `call-${sequence}`, 'echo');
  if (choice === 3) {
    return toolEvent(sequence, 'tool_result', `call-${sequence - 1}`, `result-${sequence}`);
  }
  if (choice === 4) return todoEvent(sequence, `exec-${sequence % 4}`, `todo-${sequence}`);
  if (choice === 5) return statusEvent(sequence, `exec-${sequence % 4}`, 'agent_output_chunk');
  if (choice === 6) {
    return frameworkEvent(sequence, EventType.GATE_STARTED, {
      case: 'gateStarted',
      value: {gate: GateKind.VALIDATION, recipe: `recipe-${sequence}`, command: 'bun test'},
    });
  }
  return roundOutputEvent(sequence, (sequence % 3) + 1);
}

type EventInit = Exclude<NonNullable<Parameters<typeof makeEvent>[1]>, RunEvent>;
type EventData = Exclude<EventInit['data'], {case: undefined} | undefined>;

function wireRunStatus(status: CoreRunStatus): RunStatus {
  return RunStatus[status.toUpperCase() as keyof typeof RunStatus];
}

/** The fixed test clock: `sequence` seconds after the start of 2026-01-01. */
function clockAt(sequence: number) {
  return timestampOf(new Date(Date.UTC(2026, 0, 1, 0, 0, sequence)));
}

function roundOutputEvent(sequence: number, round: number): RunEvent {
  return baseEvent(sequence, EventType.AGENT_OUTPUT_CHUNK, {
    roundLabel: `round-${round}-implementer`,
    executionId: `turn-${sequence}`,
    data: {
      case: 'agentOutputChunk',
      value: {channel: AgentOutputChannel.ASSISTANT, content: `entry ${sequence}`},
    },
  });
}

function roundToolEvent(
  sequence: number,
  kind: 'tool_call' | 'tool_result',
  callId: string,
  content: string,
): RunEvent {
  return {
    ...toolEvent(sequence, kind, callId, content),
    roundLabel: 'round-2-implementer',
  };
}

function roundFinishedEvent(sequence: number, extra: {profileSkipped?: boolean}): RunEvent {
  return baseEvent(sequence, EventType.ROUND_FINISHED, {
    roundLabel: 'round-1',
    data: {
      case: 'roundFinished',
      value: {
        attempts: 1,
        judgeVerdict: RoundJudgeVerdict.PASS,
        perfMetric: 900,
        perfUnit: 'ops/s',
        ...extra,
      },
    },
  });
}

function runStartedEvent(sequence: number): RunEvent {
  return baseEvent(sequence, EventType.RUN_STARTED, {
    data: {case: 'runStarted', value: {outerLoop: 'agent', input: '.', maxRounds: 3}},
  });
}

function baseEvent(sequence: number, type: EventType, init: EventInit = {}): RunEvent {
  return makeEvent(type, {
    sequence,
    timestamp: clockAt(sequence),
    agentKind: 'implementer',
    roundLabel: 'round-1-implementer',
    ...init,
  });
}

function chatAnswerEvent(
  sequence: number,
  answer: string,
  threadId?: string,
  invocationId?: string,
  threadTitle?: string,
): RunEvent {
  return baseEvent(sequence, EventType.CHAT, {
    agentKind: 'chat',
    roundLabel: 'experiment-chat',
    chatThreadId: threadId,
    data: {case: 'chat', value: {answer, invocationId, threadTitle}},
  });
}

/** One assistant-channel chunk of a chat turn, as the chat agent streams it. */
function chatStreamEvent(sequence: number, content: string, invocationId: string): RunEvent {
  return {
    ...outputEvent(sequence, content, invocationId),
    agentKind: 'chat',
    roundLabel: 'experiment-chat',
  };
}

function threadCreatedEvent(sequence: number, threadId: string, provider: string): RunEvent {
  return baseEvent(sequence, EventType.CHAT_THREAD_CREATED, {
    agentKind: 'chat',
    roundLabel: 'experiment-chat',
    chatThreadId: threadId,
    data: {
      case: 'chatThreadCreated',
      value: {
        threadId,
        title: '',
        driver: 'agentshim',
        provider,
        model: 'opus',
        createdAt: clockAt(sequence),
      },
    },
  });
}

function chatTitledEvent(sequence: number, threadId: string, title: string): RunEvent {
  return chatAnswerEvent(sequence, 'answer', threadId, undefined, title);
}

/** One `agent_output_chunk` on a named channel, all within a single turn. */
function channelEvent(
  sequence: number,
  channel: 'analysis' | 'diagnostic',
  content: string,
): RunEvent {
  return baseEvent(sequence, EventType.AGENT_OUTPUT_CHUNK, {
    executionId: 'turn',
    data: {
      case: 'agentOutputChunk',
      value: {
        channel:
          channel === 'analysis' ? AgentOutputChannel.ANALYSIS : AgentOutputChannel.DIAGNOSTIC,
        content,
      },
    },
  });
}

function outputEvent(sequence: number, content: string, invocationId = 'turn'): RunEvent {
  return baseEvent(sequence, EventType.AGENT_OUTPUT_CHUNK, {
    executionId: invocationId,
    data: {
      case: 'agentOutputChunk',
      value: {channel: AgentOutputChannel.ASSISTANT, content},
    },
  });
}

function executionEvent(
  sequence: number,
  type: EventType,
  executionId: string,
  data: EventData,
): RunEvent {
  return baseEvent(sequence, type, {executionId, data});
}

function statusEvent(
  sequence: number,
  executionId: string,
  kind: 'agent_output_chunk' | 'tool_call',
  status: {
    progress?: string;
    agentLabel?: string;
    elapsedSeconds?: number;
    inputTokens?: number;
    contextWindow?: number;
  } = {progress: `step ${sequence}`},
): RunEvent {
  return baseEvent(
    sequence,
    kind === 'agent_output_chunk' ? EventType.AGENT_OUTPUT_CHUNK : EventType.TOOL_CALL,
    {
      executionId,
      data:
        kind === 'agent_output_chunk'
          ? {
              case: 'agentOutputChunk',
              value: {channel: AgentOutputChannel.ANALYSIS, content: '', status},
            }
          : {
              case: 'toolCall',
              value: {tool: 'Bash', callId: `call-${sequence}`, args: {}, status},
            },
    },
  );
}

function startedData(
  assignment: string,
  identity: {driver?: string; provider?: string; model?: string} = {},
): EventData {
  return {
    case: 'agentExecutionStarted',
    value: {
      stage: 'implementation',
      attempt: 1,
      systemPrompt: '',
      userPrompt: assignment,
      activity: {mode: ExecutionActivityMode.THINKING, summary: 'Starting'},
      ...identity,
    },
  };
}

function checkpoint(
  executionId: string,
  overrides: {
    driver?: string;
    provider?: string;
    model?: string;
    startedAt?: ReturnType<typeof timestampOf>;
  } = {},
): ActiveAgentExecution {
  const [execution] = makeSnapshot({
    activeExecutions: [
      {
        executionId,
        agentKind: 'judge',
        roundLabel: 'round-2-judge',
        stage: 'judging',
        attempt: 1,
        assignment: 'Review',
        startedAt: timestampOf('2026-01-01T00:00:00Z'),
        activity: {mode: ExecutionActivityMode.THINKING, summary: 'Reviewing'},
        ...overrides,
      },
    ],
  }).activeExecutions;
  if (execution === undefined) throw new Error('checkpoint builder dropped its execution');
  return execution;
}

function toolEvent(
  sequence: number,
  kind: 'tool_call' | 'tool_result',
  callId: string,
  content: string,
): RunEvent {
  return baseEvent(sequence, kind === 'tool_call' ? EventType.TOOL_CALL : EventType.TOOL_RESULT, {
    executionId: 'turn',
    data:
      kind === 'tool_call'
        ? {case: 'toolCall', value: {tool: 'Bash', callId, args: {command: content}}}
        : {case: 'toolResult', value: {tool: 'Bash', callId, content, isError: false}},
  });
}

function todoEvent(sequence: number, executionId: string, content: string): RunEvent {
  return baseEvent(sequence, EventType.TODO_UPDATE, {
    executionId,
    data: {case: 'todoUpdate', value: {todos: [{content, status: 'in_progress'}]}},
  });
}

/** A #692 framework event: no agent kind on the wire, round context only. */
function frameworkEvent(
  sequence: number,
  type: EventType,
  data: EventData,
  overrides: EventInit = {},
): RunEvent {
  return makeEvent(type, {
    sequence,
    timestamp: clockAt(sequence),
    roundLabel: 'round-1',
    ...overrides,
    data,
  });
}

function frameworkWarningEvent(sequence: number): RunEvent {
  return frameworkEvent(
    sequence,
    EventType.FRAMEWORK_WARNING,
    {
      case: 'frameworkWarning',
      value: {
        summary: 'profiler failed',
        detail: 'nsys exited 1',
        source: FrameworkSource.LOOP,
      },
    },
    {
      diagnostic: {
        id: 'warn-1',
        code: 'framework_warning',
        summary: 'profiler failed',
        detail: 'nsys exited 1',
        scope: DiagnosticScope.RUN,
        severity: DiagnosticSeverity.WARNING,
        retryability: DiagnosticRetryability.UNKNOWN,
        source: 'loop',
      },
    },
  );
}

const DIAGNOSTIC_SEVERITY = {
  warning: DiagnosticSeverity.WARNING,
  error: DiagnosticSeverity.ERROR,
  fatal: DiagnosticSeverity.FATAL,
} as const;

function diagnosticEvent(
  sequence: number,
  type: EventType,
  id: string,
  severity: keyof typeof DIAGNOSTIC_SEVERITY,
  summary: string,
  options: {invocationId?: string; detail?: string} = {},
): RunEvent {
  return baseEvent(sequence, type, {
    executionId: options.invocationId,
    diagnostic: {
      id,
      code: 'agent_failed',
      summary,
      detail: options.detail,
      scope: DiagnosticScope.INVOCATION,
      severity: DIAGNOSTIC_SEVERITY[severity],
      retryability: DiagnosticRetryability.MANUAL,
    },
  });
}
