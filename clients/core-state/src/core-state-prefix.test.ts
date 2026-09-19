import {describe, expect, it} from 'bun:test';
import {
  AgentOutputChannel,
  DiagnosticRetryability,
  DiagnosticScope,
  DiagnosticSeverity,
  EventStatus,
  EventType,
  ExecutionActivityMode,
  ExperimentsChangeReason,
  JudgeVerdict,
  RoundJudgeVerdict,
  type RunEvent,
} from '@vibesys/backend-client';
import {makeEvent, makeSnapshot, timestampOf} from '@vibesys/backend-client/testing';
import {
  type CoreState,
  DEFAULT_CHAT_THREAD_ID,
  initialCoreState,
  reduceEventBatch,
  reduceEventPrefix,
} from './core-state.js';

/**
 * Equivalence harness for the tail bootstrap.
 *
 * The property under test: folding a whole event log in one batch must produce
 * the same `CoreState` as folding its tail and then backfilling the preceding
 * chunks through `reduceEventPrefix`.
 *
 * Two divergences are inherent to folding a bare suffix, and each has a
 * test of its own below rather than a normalization here:
 * - `status` and the expected-phase seeding both need `run_started`, which a
 *   suffix does not carry.
 * - The typed-tool latch is stateful, so a producer that emits both typed tool
 *   events and legacy `tool`-channel chunks leaks the legacy chunks a suffix
 *   sees before its first typed event.
 */

describe('prefix backfill equivalence', () => {
  for (const seed of [1, 7, 42, 1234]) {
    for (const typedTools of [true, false]) {
      const producer = typedTools ? 'typed tool events' : 'legacy tool chunks';
      it(`reproduces a full fold from a tail plus backfilled chunks (seed ${seed}, ${producer})`, () => {
        const events = generateRunEvents(seed, {typedTools});
        const full = reduceEventBatch(initialCoreState(), events);

        for (const tail of [1, 23, 137, 400, events.length - 1, events.length]) {
          const bootstrapped = backfill(events, tail, 97);

          expect(bootstrapped.historyAfterSequence).toBe(0);
          expect(bootstrapped).toEqual(full);
        }
      });
    }
  }

  it('reproduces a full fold exactly, timings included, on round boundaries', () => {
    const events = generateRunEvents(9, {typedTools: true});
    const boundaries = events.flatMap((event, index) =>
      event.type === EventType.ROUND_FINISHED ? [index + 1] : [],
    );
    expect(boundaries.length).toBeGreaterThan(2);

    const full = reduceEventBatch(initialCoreState(), events);
    const bootstrapped = backfillAt(events, boundaries);

    expect(bootstrapped).toEqual(full);
  });

  it('keeps the tail fold a suffix of the full transcript at a round boundary', () => {
    const events = generateRunEvents(3, {typedTools: true});
    const roundEnds = events.flatMap((event, index) =>
      event.type === EventType.ROUND_FINISHED ? [index + 1] : [],
    );
    // One round short of the end, so the tail carries real transcript content.
    const boundary = roundEnds.at(-2) as number;
    const floor = sequenceAt(events, boundary - 1);

    const full = reduceEventBatch(initialCoreState(), events);
    const tail = reduceEventBatch(
      initialCoreState(),
      events.slice(boundary),
      undefined,
      undefined,
      floor,
    );

    // A round boundary splits no merged entry, so the tail's transcript is a
    // plain suffix. At an arbitrary boundary it is not: the entry the boundary
    // falls inside is split in two, which is why `mergeTranscriptPrefix`
    // re-folds instead of concatenating.
    const carried = full.transcript.filter(entry => Number(entry.id) <= floor).length;
    expect(tail.transcript.length).toBeGreaterThan(0);
    expect(tail.transcript).toEqual(full.transcript.slice(carried));
    expect(tail.historyAfterSequence).toBe(floor);
  });

  it('leaves the history floor alone for callers that do not pass one', () => {
    const bootstrapped = reduceEventBatch(initialCoreState(), [chunkEvent(1)], undefined, 1, 40);
    const extended = reduceEventBatch(bootstrapped, [chunkEvent(41)], [], 41);

    expect(bootstrapped.historyAfterSequence).toBe(40);
    expect(extended.historyAfterSequence).toBe(40);
  });
});

describe('prefix merges across the chunk boundary', () => {
  it('replays an equal-sequence prefix entry before the suffix entry', () => {
    const suffixEntry = {
      id: '1',
      kind: 'assistant' as const,
      content: 'suffix',
      turnId: 'turn',
      invocationId: 'turn',
    };
    const suffix = {
      ...initialCoreState(),
      sequence: 1,
      transcript: [suffixEntry],
      historyAfterSequence: 1,
    };

    const merged = reduceEventPrefix(suffix, [chunkEvent(1, 'prefix ')], 0);

    expect(merged.transcript).toEqual([
      {
        id: '1',
        kind: 'assistant',
        content: 'prefix suffix',
        label: 'implementer · round-1-implementer',
        agentKind: 'implementer',
        roundLabel: 'round-1-implementer',
        roundNumber: 1,
        turnId: 'turn',
        invocationId: 'turn',
      },
    ]);
  });

  it('keeps the newest per-execution status across tail replay and prefix backfill', () => {
    const events = [
      executionStartedEvent(1, 'active'),
      executionStatusEvent(2, 'active', 'agent_output_chunk', {
        progress: 'Inspecting',
        agentLabel: 'Implementer',
        elapsedSeconds: 2,
        inputTokens: 4_000,
        contextWindow: 200_000,
      }),
      executionStatusEvent(3, 'active', 'tool_call', {inputTokens: 9_000}),
    ];
    const {activeExecutions: checkpoint} = makeSnapshot({
      activeExecutions: [
        {
          executionId: 'active',
          agentKind: 'implementer',
          roundLabel: 'round-1-implementer',
          stage: 'implementation',
          attempt: 1,
          assignment: 'Implement',
          startedAt: timestamp(1),
          activity: {mode: ExecutionActivityMode.THINKING, summary: 'Working'},
        },
      ],
    });
    const full = reduceEventBatch(initialCoreState(), events, checkpoint, 3);
    const tail = reduceEventBatch(initialCoreState(), events.slice(2), checkpoint, 3, 2);
    const merged = reduceEventPrefix(tail, events.slice(0, 2), 0);

    expect(merged.executionStatuses).toEqual(full.executionStatuses);
    expect(merged.executionStatuses['active']).toMatchObject({
      sequence: 3,
      progress: 'Inspecting',
      agentLabel: 'Implementer',
      inputTokens: 9_000,
      contextWindow: 200_000,
    });
    expect(merged.usage).toEqual(full.usage);
    expect(merged.usage).toEqual({inputTokens: 9_000, contextWindow: 200_000, model: null});
  });

  it('does not fill a restarted execution from an older generation with the same id', () => {
    const older = [
      executionStartedEvent(1, 'reused'),
      executionStatusEvent(2, 'reused', 'agent_output_chunk', {
        progress: 'Old work',
        agentLabel: 'Old implementer',
        contextWindow: 200_000,
      }),
    ];
    const tail = [
      executionStartedEvent(4, 'reused'),
      executionStatusEvent(5, 'reused', 'tool_call', {inputTokens: 7_000}),
    ];
    const full = reduceEventBatch(initialCoreState(), [
      ...older,
      executionFinishedEvent(3, 'reused'),
      ...tail,
    ]);
    const merged = reduceEventPrefix(reduceEventBatch(initialCoreState(), tail), older, 0);

    expect(merged.executionStatuses).toEqual(full.executionStatuses);
    expect(merged.executionStatuses['reused']).toMatchObject({
      sequence: 5,
      progress: null,
      agentLabel: null,
      inputTokens: 7_000,
      contextWindow: null,
    });
  });

  it('does not fill terminal usage from an older generation with the same id', () => {
    const older = [
      executionStartedEvent(1, 'reused'),
      executionStatusEvent(2, 'reused', 'agent_output_chunk', {
        inputTokens: 4_000,
        contextWindow: 200_000,
      }),
    ];
    const tail = [
      executionStartedEvent(4, 'reused'),
      executionStatusEvent(5, 'reused', 'tool_call', {inputTokens: 7_000}),
      executionFinishedEvent(6, 'reused'),
    ];
    const full = reduceEventBatch(initialCoreState(), [
      ...older,
      executionFinishedEvent(3, 'reused'),
      ...tail,
    ]);
    const merged = reduceEventPrefix(reduceEventBatch(initialCoreState(), tail), older, 0);

    expect(merged.executionStatuses).toEqual({});
    expect(merged.usage).toEqual(full.usage);
    expect(merged.usage).toEqual({inputTokens: 7_000, contextWindow: null, model: null});
  });

  for (const ending of ['execution finish', 'run failure'] as const) {
    it(`restores status-derived usage across prefix backfill after ${ending}`, () => {
      const older = [
        executionStartedEvent(1, 'active'),
        executionStatusEvent(2, 'active', 'agent_output_chunk', {
          progress: 'Inspecting',
          agentLabel: 'Implementer',
          inputTokens: 4_000,
          contextWindow: 200_000,
        }),
      ];
      const terminal =
        ending === 'execution finish'
          ? executionFinishedEvent(4, 'active')
          : baseEvent(4, EventType.RUN_FAILED, {
              agentKind: undefined,
              roundLabel: undefined,
              status: EventStatus.FAILED,
            });
      const tail = [executionStatusEvent(3, 'active', 'tool_call', {inputTokens: 9_000}), terminal];
      const full = reduceEventBatch(initialCoreState(), [...older, ...tail]);
      const merged = reduceEventPrefix(reduceEventBatch(initialCoreState(), tail), older, 0);

      expect(merged.executionStatuses).toEqual({});
      expect(merged.usage).toEqual(full.usage);
      expect(merged.usage).toEqual({inputTokens: 9_000, contextWindow: 200_000, model: null});
    });
  }

  it('retains an earlier usage model when a later status owns terminal usage', () => {
    const events: RunEvent[] = [
      executionStartedEvent(1, 'active'),
      baseEvent(2, EventType.USAGE_UPDATE, {
        executionId: 'active',
        data: {
          case: 'usageUpdate',
          value: {inputTokens: 4_000, contextWindow: 200_000, model: 'gpt-5.6-sol'},
        },
      }),
      executionStatusEvent(3, 'active', 'agent_output_chunk', {
        inputTokens: 9_000,
        contextWindow: 200_000,
      }),
      executionFinishedEvent(4, 'active'),
      baseEvent(5, EventType.RUN_FINISHED, {
        agentKind: undefined,
        roundLabel: undefined,
        status: EventStatus.COMPLETED,
      }),
    ];
    const full = reduceEventBatch(initialCoreState(), events, [], 5);
    const merged = backfill(events, 1, 1);

    expect(merged.executionStatuses).toEqual({});
    expect(merged.usage).toEqual(full.usage);
    expect(merged.usage).toEqual({
      inputTokens: 9_000,
      contextWindow: 200_000,
      model: 'gpt-5.6-sol',
    });
  });

  it('keeps an explicit null model from the newest preceding usage update', () => {
    const events: RunEvent[] = [
      executionStartedEvent(1, 'active'),
      baseEvent(2, EventType.USAGE_UPDATE, {
        executionId: 'active',
        data: {
          case: 'usageUpdate',
          value: {inputTokens: 4_000, contextWindow: 200_000, model: 'older-model'},
        },
      }),
      baseEvent(3, EventType.USAGE_UPDATE, {
        executionId: 'active',
        data: {
          case: 'usageUpdate',
          value: {inputTokens: 5_000, contextWindow: 200_000, model: undefined},
        },
      }),
      executionStatusEvent(4, 'active', 'agent_output_chunk', {
        inputTokens: 9_000,
        contextWindow: 200_000,
      }),
      executionFinishedEvent(5, 'active'),
      baseEvent(6, EventType.RUN_FINISHED, {
        agentKind: undefined,
        roundLabel: undefined,
        status: EventStatus.COMPLETED,
      }),
    ];
    const full = reduceEventBatch(initialCoreState(), events, [], 6);
    const merged = backfill(events, 1, 1);

    expect(merged.usage).toEqual(full.usage);
    expect(merged.usage).toEqual({inputTokens: 9_000, contextWindow: 200_000, model: null});
  });

  it('does not mutate status provenance shared by divergent prefix forks', () => {
    const tailEvents = [
      executionStatusEvent(4, 'active', 'agent_output_chunk', {inputTokens: 9_000}),
    ];
    const tail = reduceEventBatch(initialCoreState(), tailEvents, undefined, undefined, 3);
    const pristineTail = reduceEventBatch(initialCoreState(), tailEvents, undefined, undefined, 3);

    reduceEventPrefix(tail, [executionStartedEvent(3, 'active')], 2);
    const olderStatus = [
      executionStatusEvent(2, 'active', 'agent_output_chunk', {
        inputTokens: 4_000,
        contextWindow: 200_000,
      }),
    ];
    const forked = reduceEventPrefix(tail, olderStatus, 1);
    const pristine = reduceEventPrefix(pristineTail, olderStatus, 1);

    expect(forked.usage).toEqual(pristine.usage);
    expect(forked.usage?.contextWindow).toBe(200_000);
  });

  it('replays a resumed lifetime when backfill follows the server spine', () => {
    const events = [
      runStartedEvent(1),
      executionStartedEvent(2, 'old-attempt'),
      chunkEvent(3, 'old attempt started'),
      chunkEvent(4, 'old attempt still running'),
      chunkEvent(5, 'old attempt still running'),
      chunkEvent(6, 'old attempt last output'),
      {...runStartedEvent(7), timestamp: timestamp(7)},
      executionStartedEvent(8, 'resumed-attempt'),
      chunkEvent(9, 'tail after resume'),
    ];
    const floor = 8;
    const spine = events.filter(
      event => event.sequence <= floor && event.type === EventType.RUN_STARTED,
    );
    const tail = [...spine, ...events.filter(event => event.sequence > floor)];

    const full = reduceEventBatch(initialCoreState(), events);
    const bootstrapped = reduceEventBatch(initialCoreState(), tail, undefined, undefined, floor);
    // This mirrors two event-history requests. The first carries the last
    // pre-resume output and resumed start, while the old start arrives only in
    // the later request. Both run boundaries are omitted as spine duplicates.
    const afterFirstBackfill = reduceEventPrefix(
      bootstrapped,
      events.filter(
        event =>
          event.sequence === 4 ||
          event.sequence === 5 ||
          event.sequence === 6 ||
          event.sequence === 8,
      ),
      3,
    );
    const merged = reduceEventPrefix(
      afterFirstBackfill,
      events.filter(event => event.sequence === 2 || event.sequence === 3),
      0,
    );

    // `activeExecutions` is a checkpoint projection, intentionally owned by
    // the server rather than event history. Every replay-derived field agrees.
    expect({...merged, activeExecutions: {}}).toEqual({...full, activeExecutions: {}});
    expect(merged.phases.filter(phase => phase.kind === 'implementer')).toMatchObject([
      {executionId: 'old-attempt', status: 'interrupted'},
      {executionId: 'resumed-attempt', status: 'active'},
    ]);
    expect(merged.phases.find(phase => phase.executionId === 'old-attempt')?.finishedAt).toBe(
      iso(6),
    );
    expect(reduceEventPrefix(merged, [], 0)).toEqual(merged);
  });

  it('preserves each inferred endpoint across multi-resume backfill chunkings', () => {
    const events = [
      runStartedEvent(1),
      executionStartedEvent(2, 'first-attempt'),
      chunkEvent(3, 'first attempt last output'),
      runStartedEvent(4),
      executionStartedEvent(5, 'middle-attempt'),
      chunkEvent(6, 'middle attempt last output'),
      runStartedEvent(7),
      executionStartedEvent(8, 'latest-attempt'),
      chunkEvent(9, 'latest attempt output'),
    ];
    const full = reduceEventBatch(initialCoreState(), events);

    for (const chunks of [
      [
        {sequences: [5, 6, 8], floor: 4},
        {sequences: [2, 3], floor: 0},
      ],
      [
        {sequences: [8], floor: 6},
        {sequences: [5, 6], floor: 4},
        {sequences: [2, 3], floor: 0},
      ],
    ]) {
      const merged = foldWithSpineBackfill(events, 8, chunks);

      expect({...merged, activeExecutions: {}}).toEqual({...full, activeExecutions: {}});
      expect(merged.rounds).toMatchObject([
        {
          status: 'failed',
          finishedAt: iso(3),
          agentIntervals: [
            {startedAt: iso(2), finishedAt: iso(3)},
            {startedAt: iso(5), finishedAt: iso(6)},
          ],
          activeAgentStarts: {'implementer:latest-attempt': iso(8)},
        },
      ]);
    }
  });

  it('lets an explicit round finish supersede an inferred resume endpoint', () => {
    const events = [
      runStartedEvent(1),
      executionStartedEvent(2, 'first-attempt'),
      chunkEvent(3, 'first attempt last output'),
      runStartedEvent(4),
      executionStartedEvent(5, 'latest-attempt'),
      chunkEvent(6, 'latest attempt output'),
      roundFinishedEvent(7),
    ];
    const full = reduceEventBatch(initialCoreState(), events);
    const merged = foldWithSpineBackfill(events, 4, [
      {sequences: [2, 3], floor: 0},
      {sequences: [], floor: 0},
    ]);

    expect({...merged, activeExecutions: {}}).toEqual({...full, activeExecutions: {}});
    expect(merged.rounds).toMatchObject([
      {status: 'completed', finishedAt: iso(7), closedByRoundFinished: true},
    ]);
  });

  for (const [terminal, expectedStatus] of [
    ['run_failed', 'failed'],
    ['run_interrupted', 'interrupted'],
    ['run_finished', 'interrupted'],
  ] as const) {
    it(`retains ${terminal} before the resumed start`, () => {
      const events: RunEvent[] = [
        runStartedEvent(1),
        executionStartedEvent(2, 'old-attempt'),
        chunkEvent(3, 'old attempt last output'),
        makeEvent(TERMINAL_TYPES[terminal], {sequence: 4, timestamp: timestamp(4)}),
        runStartedEvent(5),
        executionStartedEvent(6, 'resumed-attempt'),
        chunkEvent(7, 'tail after resume'),
      ];
      const floor = 6;
      const spine = events.filter(
        event =>
          event.sequence <= floor &&
          [
            EventType.RUN_STARTED,
            EventType.RUN_FINISHED,
            EventType.RUN_FAILED,
            EventType.RUN_INTERRUPTED,
          ].includes(event.type),
      );
      const tail = [...spine, ...events.filter(event => event.sequence > floor)];
      const full = reduceEventBatch(initialCoreState(), events);
      const bootstrapped = reduceEventBatch(initialCoreState(), tail, undefined, undefined, floor);
      const merged = reduceEventPrefix(
        bootstrapped,
        events.filter(
          event => event.sequence === 2 || event.sequence === 3 || event.sequence === 6,
        ),
        0,
      );

      expect({...merged, activeExecutions: {}}).toEqual({...full, activeExecutions: {}});
      expect(merged.phases.find(phase => phase.executionId === 'old-attempt')).toMatchObject({
        status: expectedStatus,
        finishedAt: iso(4),
      });
    });
  }

  it('lands a tail tool result on a tool call from the chunk', () => {
    const events = [
      toolCallEvent(1, 'call-a'),
      toolCallEvent(2, 'call-b'),
      toolResultEvent(3, 'call-b', 'second result'),
      toolResultEvent(4, 'call-a', 'first result'),
    ];

    const merged = foldAsPrefix(events, 2);

    expect(merged).toEqual(reduceEventBatch(initialCoreState(), events));
    expect(merged.transcript).toHaveLength(2);
    expect(merged.transcript[0]).toMatchObject({
      id: '1',
      kind: 'tool',
      toolName: 'Bash',
      toolCallId: 'call-a',
      toolArguments: {command: 'call-a'},
      toolResult: {callId: 'call-a', content: 'first result'},
    });
    expect(merged.transcript.map(entry => entry.toolResult?.content)).toEqual([
      'first result',
      'second result',
    ]);
  });

  it('concatenates streamed assistant text split across the boundary', () => {
    const events = [
      chunkEvent(1, 'hello '),
      chunkEvent(2, 'brave '),
      chunkEvent(3, 'new '),
      chunkEvent(4, 'world'),
    ];

    const merged = foldAsPrefix(events, 2);

    expect(merged).toEqual(reduceEventBatch(initialCoreState(), events));
    expect(merged.transcript.map(entry => entry.content)).toEqual(['hello brave new world']);
  });

  it('titles a chunk-created chat thread from a tail turn', () => {
    const events = [
      threadCreatedEvent(1, 'thread-a'),
      chatEvent(2, 'thread-a', 'first answer'),
      chatEvent(3, 'thread-a', 'second answer', 'Ring buffer sizing'),
    ];

    const merged = foldAsPrefix(events, 2);

    expect(merged).toEqual(reduceEventBatch(initialCoreState(), events));
    expect(merged.chatThreads).toEqual([
      {id: DEFAULT_CHAT_THREAD_ID, title: '', driver: null, provider: null, model: null},
      {
        id: 'thread-a',
        title: 'Ring buffer sizing',
        driver: 'agentshim',
        provider: 'anthropic',
        model: 'opus',
      },
    ]);
    // Each chat answer is a complete turn and stays a separate entry. The point
    // is that the prefix fold agrees with the in-batch fold even when the two
    // answers land on opposite sides of the boundary.
    expect(merged.chatTranscripts['thread-a']?.map(entry => entry.content)).toEqual([
      'first answer',
      'second answer',
    ]);
  });

  it('folds a tail answer over a streamed chat turn left in the chunk', () => {
    const events = [
      threadCreatedEvent(1, 'thread-a'),
      chatChunkEvent(2, 'thread-a', 'partial '),
      chatChunkEvent(3, 'thread-a', 'stream'),
      chatEvent(4, 'thread-a', 'complete answer'),
    ];

    // The turn's streamed chunks fall below the floor; only its terminal answer
    // lands in the tail. A full replay folds the answer over its streamed turn
    // and renders it once, so the backfilled merge must too rather than leaving
    // the streamed entry beside a second answer entry.
    const merged = foldAsPrefix(events, 3);

    expect(merged).toEqual(reduceEventBatch(initialCoreState(), events));
    expect(merged.chatTranscripts['thread-a']).toHaveLength(1);
    expect(merged.chatTranscripts['thread-a']?.[0]).toMatchObject({
      id: '2',
      content: 'complete answer',
      invocationId: 'thread-a-turn',
    });
  });

  it('keeps an abandoned streamed turn distinct from the tail answer after it', () => {
    const events = [
      threadCreatedEvent(1, 'thread-a'),
      // exec-a streamed and then failed before recording a terminal answer.
      chatChunkEvent(2, 'thread-a', 'abandoned ', 'exec-a'),
      chatChunkEvent(3, 'thread-a', 'stream', 'exec-a'),
      chatChunkEvent(4, 'thread-a', 'complete answer', 'exec-b'),
      chatEvent(5, 'thread-a', 'complete answer', undefined, 'exec-b'),
    ];

    // The abandoned turn's chunks fall below the floor while the answered
    // turn lands in the tail. A full replay keeps two entries, so the
    // backfilled merge must not fold the tail's answer over the abandoned
    // turn it never owned.
    const merged = foldAsPrefix(events, 3);

    expect(merged).toEqual(reduceEventBatch(initialCoreState(), events));
    expect(merged.chatTranscripts['thread-a']?.map(entry => entry.content)).toEqual([
      'abandoned stream',
      'complete answer',
    ]);
  });

  it('keeps a round started in the chunk and finished in the tail', () => {
    const events = [chunkEvent(1, 'work'), roundFinishedEvent(2)];

    const merged = foldAsPrefix(events, 1);

    expect(merged).toEqual(reduceEventBatch(initialCoreState(), events));
    expect(merged.rounds).toEqual([
      {
        number: 1,
        status: 'completed',
        startedAt: iso(1),
        finishedAt: iso(2),
        closedByRoundFinished: true,
        agentIntervals: [],
        activeAgentStarts: {},
      },
    ]);
  });

  it('keeps the carried-forward flag on a round the chunk finished', () => {
    const events = [
      chunkEvent(1, 'work'),
      roundFinishedEvent(2, {profileSkipped: true}),
      {...chunkEvent(3, 'next'), roundLabel: 'round-2-implementer'},
    ];

    // The flag lands in the chunk fold; the tail only touches round 2, so the
    // boundary merge must hand round 1 through with the flag intact.
    const merged = foldAsPrefix(events, 2);

    expect(merged).toEqual(reduceEventBatch(initialCoreState(), events));
    expect(merged.rounds[0]?.profileSkipped).toBe(true);
    expect(merged.rounds[1]?.profileSkipped).toBeUndefined();
  });

  it('merges a chunk diagnostic with its tail update by id', () => {
    const events = [
      diagnosticEvent(1, 'diag-1', DiagnosticSeverity.WARNING, 'Agent stalled', undefined),
      diagnosticEvent(2, 'diag-1', DiagnosticSeverity.ERROR, 'Agent failed', 'exit 1'),
    ];

    const merged = foldAsPrefix(events, 1);

    expect(merged).toEqual(reduceEventBatch(initialCoreState(), events));
    expect(merged.diagnostics).toHaveLength(1);
    expect(merged.diagnostics[0]).toMatchObject({
      severity: 'error',
      summary: 'Agent failed',
      detail: 'exit 1',
      sequence: 2,
    });
  });

  it('replaces a chunk todo list from the tail and keeps unrelated ones', () => {
    const events = [
      todoEvent(1, 'exec-a', 'chunk plan'),
      todoEvent(2, 'exec-b', 'other plan'),
      todoEvent(3, 'exec-a', 'tail plan'),
    ];

    const merged = foldAsPrefix(events, 1);

    expect(merged).toEqual(reduceEventBatch(initialCoreState(), events));
    expect(merged.todos.map(todo => [todo.executionId, todo.items[0]?.content])).toEqual([
      ['exec-b', 'other plan'],
      ['exec-a', 'tail plan'],
    ]);
  });

  it('reconciles the timing interval of an execution split across the boundary', () => {
    const events = [
      executionStartedEvent(1, 'exec-a'),
      executionFinishedEvent(2, 'exec-a'),
      roundFinishedEvent(3),
    ];
    const full = reduceEventBatch(initialCoreState(), events);

    const merged = foldAsPrefix(events, 1);

    expect(merged).toEqual(full);
    expect(full.rounds[0]?.agentIntervals).toEqual([{startedAt: iso(1), finishedAt: iso(2)}]);
    expect(merged.rounds[0]?.agentIntervals).toEqual([{startedAt: iso(1), finishedAt: iso(2)}]);
    expect(merged.rounds[0]?.activeAgentStarts).toEqual({});
  });

  for (const terminal of ['round_finished', 'run_failed', 'run_interrupted'] as const) {
    it(`closes a backfilled active timing at a tail ${terminal}`, () => {
      const terminalEvent: RunEvent =
        terminal === 'round_finished'
          ? roundFinishedEvent(2)
          : makeEvent(TERMINAL_TYPES[terminal], {sequence: 2, timestamp: timestamp(2)});
      const events = [executionStartedEvent(1, 'exec-a'), terminalEvent];

      const merged = foldAsPrefix(events, 1);
      const full = reduceEventBatch(initialCoreState(), events);

      expect({...merged, activeExecutions: {}}).toEqual({...full, activeExecutions: {}});
      expect(merged.rounds[0]?.agentIntervals).toEqual([{startedAt: iso(1), finishedAt: iso(2)}]);
      expect(merged.rounds[0]?.activeAgentStarts).toEqual({});
    });
  }

  it('deduplicates modern and compatibility finishes split across chunks', () => {
    const events = [
      executionStartedEvent(1, 'exec-a'),
      executionFinishedEvent(2, 'exec-a'),
      phaseFinishedEvent(3, 'exec-a'),
      roundFinishedEvent(4),
    ];

    const merged = backfillAt(events, [1, 2, 3]);
    const full = reduceEventBatch(initialCoreState(), events);

    expect(merged.rounds).toEqual(full.rounds);
    expect(merged.rounds[0]?.agentIntervals).toEqual([{startedAt: iso(1), finishedAt: iso(2)}]);
  });

  it('reconciles a reused legacy key across more than two prefix chunks', () => {
    const sharedTimestamp = timestamp(9);
    const sharedIso = iso(9);
    const events = [
      {...legacyExecutionEvent(1, 'agent_execution_started'), timestamp: sharedTimestamp},
      {...legacyExecutionEvent(2, 'agent_execution_finished'), timestamp: sharedTimestamp},
      {...legacyExecutionEvent(3, 'agent_execution_started'), timestamp: sharedTimestamp},
    ];

    const merged = backfillAt(events, [1, 2]);
    const full = reduceEventBatch(initialCoreState(), events);

    expect(merged.rounds).toEqual(full.rounds);
    expect(merged.rounds[0]?.agentIntervals).toEqual([
      {startedAt: sharedIso, finishedAt: sharedIso},
    ]);
    expect(merged.rounds[0]?.activeAgentStarts).toEqual({'implementer:': sharedIso});
  });

  it('retains timing provenance when profileSkipped copies the round', () => {
    const events = [
      executionStartedEvent(1, 'exec-a'),
      roundFinishedEvent(2, {profileSkipped: true}),
    ];

    const merged = foldAsPrefix(events, 1);

    const full = reduceEventBatch(initialCoreState(), events);
    expect({...merged, activeExecutions: {}}).toEqual({...full, activeExecutions: {}});
    expect(merged.rounds[0]).toMatchObject({
      profileSkipped: true,
      agentIntervals: [{startedAt: iso(1), finishedAt: iso(2)}],
      activeAgentStarts: {},
    });
  });

  // A bare suffix has no `run_started`, so the tail fold has no run status and
  // no outer loop to seed a round's expected roles from. Both come back once the
  // server carries the run-level events into the bootstrap batch; until then the
  // merge cannot invent them, and the newer state owns run status by contract.
  it('cannot recover run status or expected-phase seeding from a bare suffix', () => {
    const events = [runStartedEvent(1), chunkEvent(2, 'work'), chunkEvent(3, ' more')];
    const full = reduceEventBatch(initialCoreState(), events);

    const merged = foldAsPrefix(events, 1);

    expect(full.status).toBe('running');
    expect(merged.status).toBe('connecting');
    expect(merged.outerLoop).toBe('plain');
    // `runStartedEvent` advertises no roles, so this also pins the legacy
    // table fallback for recordings that predate `expected_roles`.
    expect(full.expectedRoles).toBeNull();
    expect(full.phases.map(phase => phase.kind)).toEqual(['implementer', 'judge', 'perf_eval']);
    expect(merged.phases.map(phase => phase.kind)).toEqual(['implementer']);
    expect(merged.transcript.map(entry => entry.content)).toEqual(['work more']);
  });

  // The typed-tool latch is stateful: once a typed tool event is seen, legacy
  // `tool`-channel chunks are dropped as duplicates. A suffix starts unlatched,
  // so it keeps the legacy chunks it sees before its own first typed event, and
  // no state-level merge can take them back out.
  it('leaks legacy tool chunks a mixed producer emitted before the tail latched', () => {
    const events = [
      toolCallEvent(1, 'call-a'),
      toolResultEvent(2, 'call-a', 'first result'),
      legacyToolChunkEvent(3, 'duplicate of the next call'),
      toolCallEvent(4, 'call-b'),
      toolResultEvent(5, 'call-b', 'second result'),
    ];
    const full = reduceEventBatch(initialCoreState(), events);

    const merged = foldAsPrefix(events, 2);

    expect(full.transcript).toHaveLength(2);
    expect(merged.transcript).toHaveLength(3);
    expect(merged.transcript[1]?.content).toBe('duplicate of the next call');
  });
});

/** Folds the tail of `events` and backfills the rest in `chunk`-sized steps. */
function backfill(events: readonly RunEvent[], tail: number, chunk: number): CoreState {
  const start = Math.max(0, events.length - tail);
  const boundaries: number[] = [];
  for (let at = start; at > 0; at -= chunk) boundaries.unshift(at);
  return backfillAt(events, boundaries);
}

/**
 * Folds the events after the last boundary, then each preceding chunk as a
 * prefix, so the history floor walks back down to zero.
 */
function backfillAt(events: readonly RunEvent[], boundaries: readonly number[]): CoreState {
  const edges = [...new Set(boundaries)].sort((left, right) => left - right);
  const start = edges.at(-1) ?? 0;
  let state = reduceEventBatch(
    initialCoreState(),
    events.slice(start),
    undefined,
    undefined,
    sequenceAt(events, start - 1),
  );
  for (let index = edges.length - 1; index >= 0; index -= 1) {
    const from = index === 0 ? 0 : (edges[index - 1] as number);
    const to = edges[index] as number;
    state = reduceEventPrefix(state, events.slice(from, to), sequenceAt(events, from - 1));
  }
  return state;
}

function foldAsPrefix(events: readonly RunEvent[], boundary: number): CoreState {
  return backfillAt(events, [boundary]);
}

function foldWithSpineBackfill(
  events: readonly RunEvent[],
  floor: number,
  chunks: readonly {sequences: readonly number[]; floor: number}[],
): CoreState {
  const spine = events.filter(
    event =>
      event.sequence <= floor &&
      (event.type === EventType.RUN_STARTED ||
        event.type === EventType.RUN_FINISHED ||
        event.type === EventType.RUN_FAILED ||
        event.type === EventType.RUN_INTERRUPTED),
  );
  let state = reduceEventBatch(
    initialCoreState(),
    [...spine, ...events.filter(event => event.sequence > floor)],
    undefined,
    undefined,
    floor,
  );
  for (const chunk of chunks) {
    state = reduceEventPrefix(
      state,
      events.filter(event => chunk.sequences.includes(event.sequence)),
      chunk.floor,
    );
  }
  return state;
}

function sequenceAt(events: readonly RunEvent[], index: number): number {
  return index < 0 ? 0 : (events[index]?.sequence ?? 0);
}

// A deterministic 32-bit generator, so a failing seed is reproducible.
class Rng {
  #state: number;

  constructor(seed: number) {
    this.#state = (seed * 2654435761) >>> 0;
  }

  float(): number {
    this.#state = (this.#state + 0x6d2b79f5) >>> 0;
    let value = this.#state;
    value = Math.imul(value ^ (value >>> 15), value | 1);
    value ^= value + Math.imul(value ^ (value >>> 7), value | 61);
    return ((value ^ (value >>> 14)) >>> 0) / 4294967296;
  }

  int(min: number, max: number): number {
    return min + Math.floor(this.float() * (max - min + 1));
  }

  pick<T>(values: readonly T[]): T {
    return values[this.int(0, values.length - 1)] as T;
  }
}

const WORDS =
  'the implementer measured allocation pressure across the hot path and adjusted the ring buffer capacity while re-checking cache line alignment for the producer and consumer cursors so false sharing stays bounded'.split(
    ' ',
  );

const AGENT_KINDS = ['orchestrator', 'implementer', 'judge', 'profiler'] as const;
const TOOLS = ['bash', 'read_file', 'edit_file', 'grep', 'write_file'] as const;
const CHANNELS = [
  AgentOutputChannel.ASSISTANT,
  AgentOutputChannel.ASSISTANT,
  AgentOutputChannel.ASSISTANT,
  AgentOutputChannel.ANALYSIS,
  AgentOutputChannel.PROMPT,
] as const;

type Timestamp = ReturnType<typeof timestampOf>;
type EventInit = NonNullable<Parameters<typeof makeEvent>[1]>;

/**
 * A synthetic run log shaped like a real one: rounds of agent executions with
 * streamed output, paired tool calls, todo and usage updates, per-round judge
 * and benchmark results, and chat traffic across several threads.
 *
 * `typedTools` picks the producer flavor: typed `tool_call`/`tool_result`
 * events, or the legacy `tool`-channel chunks a driver without structured tool
 * reporting emits. One log carries one flavor, because the reducer's
 * typed-tool latch makes a log carrying both order-dependent (see the mixed
 * producer test).
 *
 * Sized well under MAX_TRANSCRIPT_ENTRIES so cap eviction, which replay and
 * backfill are not required to agree on, never enters the comparison.
 */

// biome-ignore lint/complexity/noExcessiveCognitiveComplexity: pre-existing; tracked: #288
function generateRunEvents(seed: number, options: {typedTools: boolean}, rounds = 5): RunEvent[] {
  const rng = new Rng(seed);
  const events: RunEvent[] = [];
  const threadIds = ['thread-a', 'thread-b', 'thread-c'];
  let clock = 0;

  const emit = (type: EventType, init: EventInit = {}): void => {
    clock += rng.int(5, 400);
    events.push(makeEvent(type, {...init, sequence: events.length + 1, timestamp: isoAt(clock)}));
  };

  emit(EventType.SERVER_STARTED, {status: EventStatus.ACTIVE});
  emit(EventType.SERVER_READY, {data: {case: 'serverReady', value: {}}});
  emit(EventType.RUN_STARTED, {
    status: EventStatus.ACTIVE,
    data: {
      case: 'runStarted',
      value: {
        outerLoop: 'agent',
        input: '/synthetic/target',
        maxRounds: rounds,
        expectedRoles: [...AGENT_KINDS],
      },
    },
  });

  for (let round = 1; round <= rounds; round += 1) {
    const roundLabel = `round-${round}`;
    for (const kind of AGENT_KINDS) {
      const executionId = `exec-${round}-${kind}`;
      const prompt = words(rng, 5, 20);
      const context = {agentKind: kind, roundLabel, executionId};
      emit(EventType.AGENT_EXECUTION_STARTED, {
        ...context,
        status: EventStatus.ACTIVE,
        data: {
          case: 'agentExecutionStarted',
          value: {
            stage: kind,
            systemPrompt: prompt,
            userPrompt: prompt,
            activity: {mode: ExecutionActivityMode.THINKING, summary: 'Thinking'},
            driver: 'agentshim',
            provider: 'anthropic',
            model: 'claude-sonnet',
          },
        },
      });
      emit(EventType.PHASE_STARTED, {
        ...context,
        status: EventStatus.ACTIVE,
        data: {case: 'phase', value: {phase: kind}},
      });
      emit(EventType.INVOCATION_STARTED, {
        ...context,
        status: EventStatus.ACTIVE,
        data: {case: 'invocationStarted', value: {systemPrompt: prompt, userPrompt: prompt}},
      });

      const turns = rng.int(4, 12);
      for (let turn = 0; turn < turns; turn += 1) {
        emit(EventType.AGENT_OUTPUT_CHUNK, {
          ...context,
          data: {
            case: 'agentOutputChunk',
            value: {channel: rng.pick(CHANNELS), content: words(rng, 3, 25)},
          },
        });
        if (rng.float() < 0.35) {
          const callId = `${executionId}-call-${turn}`;
          const tool = rng.pick(TOOLS);
          if (options.typedTools) {
            emit(EventType.TOOL_CALL, {
              ...context,
              data: {case: 'toolCall', value: {tool, callId, args: {pattern: words(rng, 2, 5)}}},
            });
            emit(EventType.TOOL_RESULT, {
              ...context,
              data: {
                case: 'toolResult',
                value: {
                  tool,
                  callId,
                  content: words(rng, 5, 40),
                  isError: rng.float() < 0.05,
                },
              },
            });
          } else {
            emit(EventType.AGENT_OUTPUT_CHUNK, {
              ...context,
              data: {
                case: 'agentOutputChunk',
                value: {
                  channel: AgentOutputChannel.TOOL,
                  content: `→ ${tool} ${words(rng, 2, 5)}`,
                },
              },
            });
            emit(EventType.AGENT_OUTPUT_CHUNK, {
              ...context,
              data: {
                case: 'agentOutputChunk',
                value: {channel: AgentOutputChannel.TOOL, content: words(rng, 5, 40)},
              },
            });
          }
        }
        if (rng.float() < 0.1) {
          emit(EventType.TODO_UPDATE, {
            ...context,
            data: {
              case: 'todoUpdate',
              value: {
                todos: ['completed', 'in_progress', 'pending'].map(status => ({
                  content: words(rng, 2, 6),
                  status,
                })),
              },
            },
          });
        }
        if (rng.float() < 0.15) {
          emit(EventType.USAGE_UPDATE, {
            ...context,
            data: {
              case: 'usageUpdate',
              value: {
                inputTokens: rng.int(2000, 180000),
                contextWindow: 200000,
                model: 'claude-sonnet',
              },
            },
          });
        }
      }

      emit(EventType.AGENT_EXECUTION_FINISHED, {
        ...context,
        status: EventStatus.COMPLETED,
        data: {case: 'agentExecutionFinished', value: {}},
      });
      emit(EventType.INVOCATION_FINISHED, {
        ...context,
        status: EventStatus.COMPLETED,
        data: {case: 'invocationFinished', value: {}},
      });
      emit(EventType.PHASE_FINISHED, {
        ...context,
        status: EventStatus.COMPLETED,
        data: {case: 'phase', value: {phase: kind}},
      });
    }

    emit(EventType.JUDGE_RESULT, {
      roundLabel,
      data: {
        case: 'judgeResult',
        value: {
          verdict: rng.float() < 0.7 ? JudgeVerdict.PASS : JudgeVerdict.FAIL,
          feedback: words(rng, 5, 20),
          attempt: 1,
        },
      },
    });
    emit(EventType.BENCHMARK_RESULT, {
      roundLabel,
      data: {
        case: 'benchmarkResult',
        value: {metric: 'throughput', value: rng.int(100000, 5000000), unit: 'ops/s'},
      },
    });
    emit(EventType.ROUND_FINISHED, {
      roundLabel,
      status: EventStatus.COMPLETED,
      data: {
        case: 'roundFinished',
        value: {
          attempts: 1,
          judgeVerdict: rng.float() < 0.7 ? RoundJudgeVerdict.PASS : RoundJudgeVerdict.FAIL,
          perfMetric: rng.int(100000, 5000000),
          perfUnit: 'ops/s',
        },
      },
    });
    if (rng.float() < 0.3) {
      emit(EventType.EXPERIMENTS_CHANGED, {
        data: {
          case: 'experimentsChanged',
          value: {reason: ExperimentsChangeReason.ROUND_PERSISTED},
        },
      });
    }
    if (rng.float() < 0.5) {
      // A thread the operator opens mid-run, titled only on a later turn, and
      // occasionally the implicit default thread with no id at all.
      const threadId = rng.float() < 0.2 ? undefined : rng.pick(threadIds);
      const chatContext = {
        agentKind: 'chat',
        roundLabel: 'experiment-chat',
        ...(threadId === undefined ? {} : {chatThreadId: threadId}),
      };
      if (threadId !== undefined) {
        emit(EventType.CHAT_THREAD_CREATED, {
          ...chatContext,
          data: {
            case: 'chatThreadCreated',
            value: {
              threadId,
              title: '',
              driver: 'agentshim',
              provider: 'anthropic',
              model: 'claude-sonnet',
              createdAt: isoAt(clock),
            },
          },
        });
      }
      // Most turns stream their answer before the terminal record. A streamed
      // turn is sometimes abandoned (its invocation failed before recording an
      // answer), and an unstreamed answer is sometimes a legacy id-less record
      // from before answers carried their invocation.
      const invocationId = `chat-${events.length}`;
      const streamed = rng.float() < 0.6;
      if (streamed) {
        for (let chunk = rng.int(1, 3); chunk > 0; chunk -= 1) {
          emit(EventType.AGENT_OUTPUT_CHUNK, {
            ...chatContext,
            executionId: invocationId,
            data: {
              case: 'agentOutputChunk',
              value: {channel: AgentOutputChannel.ASSISTANT, content: `${words(rng, 2, 6)} `},
            },
          });
        }
      }
      const abandoned = streamed && rng.float() < 0.25;
      if (!abandoned) {
        emit(EventType.CHAT, {
          ...chatContext,
          status: EventStatus.ANSWERED,
          data: {
            case: 'chat',
            value: {
              answer: words(rng, 5, 30),
              ...(rng.float() < 0.5 ? {threadTitle: words(rng, 2, 4)} : {}),
              ...(streamed || rng.float() < 0.5 ? {invocationId} : {}),
            },
          },
        });
      }
    }
  }

  emit(EventType.RUN_FINISHED, {status: EventStatus.COMPLETED});
  return events;
}

function words(rng: Rng, min: number, max: number): string {
  const count = rng.int(min, max);
  const chosen: string[] = [];
  for (let at = 0; at < count; at += 1) chosen.push(rng.pick(WORDS));
  return chosen.join(' ');
}

function isoAt(millis: number): Timestamp {
  return timestampOf(new Date(Date.UTC(2026, 7, 20) + millis));
}

/** The wire timestamp of event `sequence`. */
function timestamp(sequence: number): Timestamp {
  return timestampOf(new Date(Date.UTC(2026, 0, 1, 0, 0, sequence)));
}

/** The ISO string core-state derives from `timestamp(sequence)`. */
function iso(sequence: number): string {
  return new Date(Date.UTC(2026, 0, 1, 0, 0, sequence)).toISOString();
}

function baseEvent(sequence: number, type: EventType, init: EventInit = {}): RunEvent {
  return makeEvent(type, {
    sequence,
    timestamp: timestamp(sequence),
    agentKind: 'implementer',
    roundLabel: 'round-1-implementer',
    ...init,
  });
}

function chunkEvent(sequence: number, content = `entry ${sequence}`): RunEvent {
  return baseEvent(sequence, EventType.AGENT_OUTPUT_CHUNK, {
    executionId: 'turn',
    data: {case: 'agentOutputChunk', value: {channel: AgentOutputChannel.ASSISTANT, content}},
  });
}

function executionStatusEvent(
  sequence: number,
  executionId: string,
  kind: 'agent_output_chunk' | 'tool_call',
  status: {
    progress?: string;
    agentLabel?: string;
    elapsedSeconds?: number;
    inputTokens?: number;
    contextWindow?: number;
  },
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
              value: {
                channel: AgentOutputChannel.ANALYSIS,
                content: status.progress ?? '',
                status,
              },
            }
          : {case: 'toolCall', value: {tool: 'Bash', callId: `call-${sequence}`, args: {}, status}},
    },
  );
}

function toolCallEvent(sequence: number, callId: string): RunEvent {
  return baseEvent(sequence, EventType.TOOL_CALL, {
    executionId: 'turn',
    data: {case: 'toolCall', value: {tool: 'Bash', callId, args: {command: callId}}},
  });
}

function toolResultEvent(sequence: number, callId: string, content: string): RunEvent {
  return baseEvent(sequence, EventType.TOOL_RESULT, {
    executionId: 'turn',
    data: {case: 'toolResult', value: {tool: 'Bash', callId, content, isError: false}},
  });
}

function legacyToolChunkEvent(sequence: number, content: string): RunEvent {
  return baseEvent(sequence, EventType.AGENT_OUTPUT_CHUNK, {
    executionId: 'legacy-turn',
    data: {case: 'agentOutputChunk', value: {channel: AgentOutputChannel.TOOL, content}},
  });
}

function runStartedEvent(sequence: number): RunEvent {
  return makeEvent(EventType.RUN_STARTED, {
    sequence,
    timestamp: timestamp(sequence),
    status: EventStatus.ACTIVE,
    data: {case: 'runStarted', value: {outerLoop: 'plain', input: '/target', maxRounds: 3}},
  });
}

function roundFinishedEvent(sequence: number, extra: {profileSkipped?: boolean} = {}): RunEvent {
  return makeEvent(EventType.ROUND_FINISHED, {
    sequence,
    timestamp: timestamp(sequence),
    status: EventStatus.COMPLETED,
    roundLabel: 'round-1',
    data: {
      case: 'roundFinished',
      value: {attempts: 1, judgeVerdict: RoundJudgeVerdict.PASS, ...extra},
    },
  });
}

function executionStartedEvent(sequence: number, executionId: string): RunEvent {
  return baseEvent(sequence, EventType.AGENT_EXECUTION_STARTED, {
    roundLabel: 'round-1',
    executionId,
    status: EventStatus.ACTIVE,
    data: {
      case: 'agentExecutionStarted',
      value: {
        stage: 'implementation',
        systemPrompt: '',
        userPrompt: 'Implement the queue',
        activity: {mode: ExecutionActivityMode.THINKING, summary: 'Starting'},
      },
    },
  });
}

function executionFinishedEvent(sequence: number, executionId: string): RunEvent {
  return baseEvent(sequence, EventType.AGENT_EXECUTION_FINISHED, {
    roundLabel: 'round-1',
    executionId,
    status: EventStatus.COMPLETED,
    data: {case: 'agentExecutionFinished', value: {}},
  });
}

function phaseFinishedEvent(sequence: number, executionId: string): RunEvent {
  return baseEvent(sequence, EventType.PHASE_FINISHED, {
    roundLabel: 'round-1',
    executionId,
    status: EventStatus.COMPLETED,
    data: {case: 'phase', value: {phase: 'implementer'}},
  });
}

function legacyExecutionEvent(
  sequence: number,
  type: 'agent_execution_started' | 'agent_execution_finished',
): RunEvent {
  const event =
    type === 'agent_execution_started'
      ? executionStartedEvent(sequence, 'discarded')
      : executionFinishedEvent(sequence, 'discarded');
  return {...event, executionId: undefined};
}

function threadCreatedEvent(sequence: number, threadId: string): RunEvent {
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
        provider: 'anthropic',
        model: 'opus',
        createdAt: timestamp(sequence),
      },
    },
  });
}

function chatEvent(
  sequence: number,
  threadId: string,
  answer: string,
  title?: string,
  invocationId?: string,
): RunEvent {
  return baseEvent(sequence, EventType.CHAT, {
    agentKind: 'chat',
    roundLabel: 'experiment-chat',
    chatThreadId: threadId,
    status: EventStatus.ANSWERED,
    data: {case: 'chat', value: {answer, threadTitle: title, invocationId}},
  });
}

function chatChunkEvent(
  sequence: number,
  threadId: string,
  content: string,
  invocationId?: string,
): RunEvent {
  return baseEvent(sequence, EventType.AGENT_OUTPUT_CHUNK, {
    agentKind: 'chat',
    roundLabel: 'experiment-chat',
    chatThreadId: threadId,
    executionId: invocationId ?? `${threadId}-turn`,
    data: {case: 'agentOutputChunk', value: {channel: AgentOutputChannel.ASSISTANT, content}},
  });
}

function todoEvent(sequence: number, executionId: string, content: string): RunEvent {
  return baseEvent(sequence, EventType.TODO_UPDATE, {
    executionId,
    data: {case: 'todoUpdate', value: {todos: [{content, status: 'in_progress'}]}},
  });
}

function diagnosticEvent(
  sequence: number,
  id: string,
  severity: DiagnosticSeverity,
  summary: string,
  detail: string | undefined,
): RunEvent {
  return baseEvent(sequence, EventType.INVOCATION_FINISHED, {
    executionId: 'turn',
    diagnostic: {
      id,
      code: 'agent_failed',
      summary,
      detail,
      scope: DiagnosticScope.INVOCATION,
      severity,
      retryability: DiagnosticRetryability.MANUAL,
    },
  });
}

const TERMINAL_TYPES = {
  run_failed: EventType.RUN_FAILED,
  run_interrupted: EventType.RUN_INTERRUPTED,
  run_finished: EventType.RUN_FINISHED,
} as const;
