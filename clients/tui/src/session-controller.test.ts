import {afterEach, beforeEach, describe, expect, it} from 'bun:test';
import {createHash} from 'node:crypto';
import {mkdirSync, mkdtempSync, rmSync, writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {
  BackendClientError,
  type ProtocolResponse,
  type RequestInput,
  type RunEvent,
  type RunStatus,
  ServerError,
  type ServerMessage,
} from '@vibesys/contracts';
import {chatEvent, event, statusEvent} from '@vibesys/contracts/testing';
import type {
  EventSubscription,
  ScheduleTimeout,
  ServerTransport,
  SubscribeOptions,
} from '@vibesys/transport';
import {resolveStartupTrace} from './boot-trace.js';
import {fuzzyMatchCommands} from './commands.js';
import {readNote, writeNote} from './notes-store.js';
import {SocketSessionController} from './session-controller.js';
import {
  chatPaneFocused,
  chatPaneVisible,
  experimentLogVisible,
  iterationRailRounds,
} from './session-model.js';
import {rightPaneContent, rightPaneTitle} from './ui/right-pane.js';

const NOTE_PROJECT_SCOPE = 'a'.repeat(64);

function paneContent(controller: SocketSessionController): string | null {
  const pane = controller.state.layout.right;
  return pane === null ? null : rightPaneContent(controller.state, pane);
}

/** The command-bar palette's current matches, by name, for asserting on what `/help` offers. */
function paletteNames(controller: SocketSessionController): string[] {
  const context = {surface: 'command' as const, chatDocked: chatPaneVisible(controller.state)};
  return fuzzyMatchCommands('', context).map(command => command.name);
}

/** A recorded measurement, the cheapest event to observe as folded or dropped. */
function benchmark(sequence: number, value: number): RunEvent {
  return {
    sequence,
    timestamp: `2026-01-01T00:00:0${sequence}Z`,
    type: 'benchmark_result',
    round_label: 'round-1',
    data: {kind: 'benchmark_result', metric: 'ops', value, unit: 'ops/s'},
  };
}

/** How many times the perf pane has been (re)loaded from the backend. */
function performanceQueries(transport: FakeTransport): number {
  return transport.requests.filter(request => request.type === 'query.performance').length;
}

/** The chat palette's current matches, by name, for the chat-surface counterpart above. */
function chatPaletteNames(controller: SocketSessionController): string[] {
  const context = {surface: 'chat' as const, chatDocked: chatPaneVisible(controller.state)};
  return fuzzyMatchCommands('', context).map(command => command.name);
}

describe('session controller', () => {
  it('opens the palette locally without sending a backend command', async () => {
    const transport = new FakeTransport();
    const controller = new SocketSessionController(transport);

    await controller.submitCommand('/help');

    expect(controller.state.palette).not.toBeNull();
    expect(paletteNames(controller)).toContain('/open-round');
    expect(transport.requests).toEqual([]);
  });

  it('opens the palette scoped to the chat surface from /help in the chat composer', async () => {
    const transport = new FakeTransport();
    const controller = new SocketSessionController(transport);

    await controller.submitChat('/help');

    expect(controller.state.palette).not.toBeNull();
    // The chat surface leads with its own thread commands, which the command
    // bar does not register at all.
    expect(chatPaletteNames(controller)).toContain('/switch');
    expect(transport.requests).toEqual([]);
  });

  it('keeps ordinary text out of commands and accepts it from Experiment chat', async () => {
    const transport = new FakeTransport();
    const controller = new SocketSessionController(transport);

    await controller.submitCommand('what is happening?');

    expect(transport.requests).toEqual([]);
    expect(controller.state.chatConversation).toEqual([]);
    // Ordinary text is a routing mistake, not a malformed command: it lands on
    // the command input's own hint row rather than raising the shared banner
    // (#564/#635's reasoning, applied to a wrong command instead of an empty
    // one).
    expect(controller.state.inputError).toContain('Not a command:');
    expect(controller.state.errorBanner).toBeNull();

    controller.clearInputError();
    await controller.submitChat('what is happening?');

    expect(transport.requests).toEqual([{type: 'query.chat', text: 'what is happening?'}]);
    // The pane is part of the landing view, so nothing opens over the table.
    expect(controller.state.chatOpen).toBe(false);
    expect(chatPaneVisible(controller.state)).toBe(true);
    expect(experimentLogVisible(controller.state)).toBe(true);
    expect(controller.state.chatConversation).toMatchObject([
      {kind: 'user', labelKind: 'user', content: 'what is happening?'},
      {kind: 'assistant', labelKind: 'answer', content: 'The implementer is running.'},
    ]);
  });

  it('reduces replay and live events without depending on OpenTUI', async () => {
    const transport = new FakeTransport();
    const controller = new SocketSessionController(transport);
    await controller.start();

    transport.emit({type: 'event_batch', events: [event(1, 'agent_output_chunk', 'one\n')]});
    transport.emit({type: 'event', event: event(2, 'agent_output_chunk', 'two\n')});

    expect(controller.state.core.transcript.map(entry => entry.content).join('')).toBe(
      'one\ntwo\n',
    );
    expect(controller.state.core.sequence).toBe(2);
    await controller.stop();
    expect(transport.closed).toBe(true);
  });

  it('stops while the initial subscription handshake is pending', async () => {
    const transport = new FakeTransport();
    transport.deferSubscribe = true;
    const controller = new SocketSessionController(transport);
    const starting = controller.start();

    const stopping = controller.stop();

    // Stop fences event delivery, then closes the client in the same turn so
    // its pending subscribe rejects. The handshake is never released by the
    // test and no timeout advances; repeated stop callers join the same close.
    expect(transport.closeCalls).toBe(1);
    await Promise.all([starting, stopping, controller.stop()]);
    expect(transport.closeCalls).toBe(1);
    expect(transport.closed).toBe(true);
  });

  it('does not treat another tagged message carrying events as an event batch', async () => {
    const transport = new FakeTransport();
    const controller = new SocketSessionController(transport);
    await controller.start();

    // A future `subscribed` member could add observability events. Dispatch is
    // by its wire tag, never by an incidental payload field.
    const subscribedWithEvents = {
      type: 'subscribed' as const,
      request_id: 'subscription',
      run_id: 'run',
      latest_sequence: 0,
      events: [event(1, 'agent_output_chunk', 'must not fold\n')],
    };
    transport.emit(subscribedWithEvents);

    expect(controller.state.core.sequence).toBe(0);
    expect(controller.state.core.transcript).toEqual([]);
    await controller.stop();
  });

  // A chat RPC answers with the journal tail written while the request was in
  // flight, so its events can outrun the subscription by a whole poll
  // interval. Folding them used to advance the contiguous stream cursor, and
  // the batch the subscription was still holding was then dropped as already
  // folded, permanently and across reconnects.
  it('keeps the events a chat response outran, and folds its own once', async () => {
    const answer: RunEvent = {
      sequence: 4,
      timestamp: '2026-01-01T00:00:04Z',
      type: 'chat',
      agent_kind: 'chat',
      round_label: 'experiment-chat',
      data: {kind: 'chat', answer: 'the answer'},
    };
    const transport = new FakeTransport([answer]);
    const controller = new SocketSessionController(transport);
    await controller.start();
    transport.emit({type: 'event', event: event(1, 'agent_output_chunk', 'one\n')});

    await controller.submitChat('what is happening?');

    // Immediate feedback, without claiming the subscription reached sequence 4.
    expect(controller.state.chatConversation.map(entry => entry.content)).toEqual([
      'what is happening?',
      'the answer',
    ]);
    expect(controller.state.core.sequence).toBe(1);

    // The batch the subscription was holding, then its copies of the response.
    transport.emit({type: 'event', event: benchmark(2, 11)});
    transport.emit({type: 'event', event: benchmark(3, 22)});
    transport.emit({type: 'event', event: answer});

    expect(controller.state.core.benchmarks.map(record => record.value)).toEqual([11, 22]);
    expect(controller.state.core.sequence).toBe(4);
    expect(
      controller.state.core.chatTranscript.filter(entry => entry.content === 'the answer'),
    ).toHaveLength(1);
  });

  it('issues every boot request concurrently', async () => {
    const started: string[] = [];
    const pending: Array<() => void> = [];
    const transport: ServerTransport = {
      request(input: RequestInput): Promise<ProtocolResponse> {
        started.push(input.type ?? 'untyped');
        return new Promise(resolve => {
          pending.push(() =>
            resolve({
              protocol_version: 1,
              request_id: 'request',
              timestamp: '2026-01-01T00:00:00Z',
              ok: true,
            }),
          );
        });
      },
      subscribe(): Promise<EventSubscription> {
        started.push('subscribe');
        return new Promise(resolve => {
          pending.push(() => resolve({close: () => Promise.resolve()}));
        });
      },
      close: () => Promise.resolve(),
    };
    const controller = new SocketSessionController(transport);

    const boot = controller.start();

    // The event replay is the long pole; nothing waits behind it, and nothing
    // waits behind the two independent queries either.
    expect([...started].sort()).toEqual([
      'query.experiments',
      'query.iterations',
      'query.snapshot',
      'subscribe',
    ]);
    for (const resolve of pending.splice(0)) resolve();

    // The design log deliberately rides behind the experiments answer rather
    // than the boot barrier, so it is the one follow-up request here.
    await new Promise<void>(resolve => setTimeout(resolve, 0));
    expect(started).toContain('query.design');
    for (const resolve of pending.splice(0)) resolve();
    await boot;
  });

  it('uses queried evolve generations as the rail navigation roots', async () => {
    const transport = new FakeTransport();
    transport.iterationsReady = true;
    transport.iterations = {
      kind: 'page',
      schema_version: 1,
      run_id: 'run',
      projection_revision: 'evolve-1',
      parent_id: null,
      from_cursor: null,
      items: [
        {
          id: 'generation:4',
          parent_id: null,
          kind: 'generation',
          label: 'Generation 4',
          ordinal: 4,
          status: 'active',
          child_count: 3,
          headline_metrics: [],
        },
      ],
      next_cursor: null,
    };
    const controller = new SocketSessionController(transport);

    await controller.start();

    expect(transport.requests).toContainEqual({
      type: 'query.iterations',
      expected_store_id: '',
    });
    expect(iterationRailRounds(controller.state)).toMatchObject([
      {iterationLabel: 'Generation 4', number: null, status: 'active'},
    ]);
  });

  it('keeps legacy round navigation when policy iterations are explicitly unavailable', async () => {
    const transport = new FakeTransport();
    transport.iterationsReady = true;
    transport.iterations = {
      kind: 'unavailable',
      schema_version: 1,
      reason: 'external_state_unavailable',
    };
    const controller = new SocketSessionController(transport);

    await controller.start();
    transport.emit({type: 'event', event: roundFinished(13, 1)});

    expect(controller.state.iterations.unavailable?.reason).toBe('external_state_unavailable');
    expect(iterationRailRounds(controller.state).map(round => round.number)).toEqual([1]);
  });

  it('refreshes policy navigation on a semantic iteration invalidation', async () => {
    const transport = new FakeTransport();
    transport.iterationsReady = true;
    transport.iterations = iterationPage('evolve-1', 1);
    const controller = new SocketSessionController(transport);
    await controller.start();
    transport.iterations = iterationPage('evolve-2', 2);

    transport.emit({
      type: 'event',
      event: {
        ...event(13, 'iterations_changed'),
        data: {kind: 'iterations_changed', revision: 'evolve-2'},
      },
    });
    await settleMicrotasks();

    expect(transport.requests.filter(request => request.type === 'query.iterations')).toHaveLength(
      2,
    );
    expect(iterationRailRounds(controller.state)).toMatchObject([
      {iterationLabel: 'Generation 2', status: 'completed'},
    ]);
  });

  it('suppresses an initial iteration result superseded by an invalidation', async () => {
    const transport = new FakeTransport();
    const initial = deferred<ProtocolResponse>();
    const current = deferred<ProtocolResponse>();
    transport.iterationResponses.push(initial.promise, current.promise);
    const controller = new SocketSessionController(transport);
    const revisions: Array<string | null> = [];
    controller.subscribe(state => revisions.push(state.iterations.revision));

    const started = controller.start();
    await settleMicrotasks();
    transport.emit({
      type: 'event',
      event: {
        ...event(13, 'iterations_changed'),
        data: {kind: 'iterations_changed', revision: 'current'},
      },
    });
    initial.resolve(iterationPageResponse('stale', null, 0, 1, null));
    await settleMicrotasks();

    expect(controller.state.iterations.revision).toBeNull();
    expect(revisions).not.toContain('stale');
    expect(transport.requests.filter(request => request.type === 'query.iterations')).toHaveLength(
      2,
    );

    current.resolve(iterationPageResponse('current', null, 1, 1, null));
    await started;
    expect(controller.state.iterations.revision).toBe('current');
    expect(revisions).not.toContain('stale');
    await controller.stop();
  });

  it('suppresses an initial iteration failure superseded by a successful invalidation', async () => {
    const transport = new FakeTransport();
    const initial = deferred<ProtocolResponse>();
    const current = deferred<ProtocolResponse>();
    transport.iterationResponses.push(initial.promise, current.promise);
    const controller = new SocketSessionController(transport);

    const started = controller.start();
    await settleMicrotasks();
    transport.emit({
      type: 'event',
      event: {
        ...event(13, 'iterations_changed'),
        data: {kind: 'iterations_changed', revision: 'current'},
      },
    });
    initial.reject(new ServerError('superseded iteration failure'));
    await settleMicrotasks();

    expect(controller.state.errorBanner).toBeNull();
    current.resolve(iterationPageResponse('current', null, 1, 1, null));
    await started;
    expect(controller.state.iterations.revision).toBe('current');
    expect(controller.state.errorBanner).toBeNull();
    await controller.stop();
  });

  it('loads iteration navigation again when the stream store replaces an in-flight query', async () => {
    const transport = new FakeTransport();
    const stale = deferred<ProtocolResponse>();
    const current = deferred<ProtocolResponse>();
    transport.iterationResponses.push(stale.promise, current.promise);
    const controller = new SocketSessionController(transport);

    const started = controller.start();
    await settleMicrotasks();
    transport.emit({
      type: 'event_batch',
      events: [event(1, 'server_ready')],
      store_id: 'bootstrap-store',
      history_after_sequence: 0,
    });
    transport.emit({
      type: 'event_batch',
      events: [
        {
          ...event(1, 'run_started'),
          data: {kind: 'run_started', outer_loop: 'evolve', input: '.', max_rounds: 3},
        },
      ],
      store_id: 'run-store',
      history_after_sequence: 0,
      rebootstrap: true,
    });

    stale.resolve(iterationPageResponse('bootstrap', null, 0, 1, null));
    await settleMicrotasks();
    expect(controller.state.iterations.revision).toBeNull();
    expect(transport.requests.filter(request => request.type === 'query.iterations')).toHaveLength(
      2,
    );

    current.resolve(iterationPageResponse('run', null, 1, 1, null));
    await started;
    expect(controller.state.iterations.revision).toBe('run');
    await controller.stop();
  });

  it('suppresses a fenced iteration rejection until the stream proves the next store', async () => {
    const transport = new FakeTransport();
    transport.iterationsReady = true;
    transport.iterations = iterationPage('store-a', 1);
    const controller = new SocketSessionController(transport);
    await controller.start();
    transport.iterationResponses.push(
      new ServerError('The run event store changed before iterations were captured', {
        code: 'iteration_store_replaced',
        summary: 'The run event store changed before iterations were captured',
        scope: 'request',
      }),
      iterationPageResponse('store-c', null, 3, 1, null),
    );

    transport.emit({
      type: 'event_batch',
      events: [event(1, 'server_ready')],
      store_id: 'store-2',
      history_after_sequence: 0,
      rebootstrap: true,
    });
    await settleMicrotasks();

    expect(controller.state.iterations.revision).toBeNull();
    expect(controller.state.errorBanner).toBeNull();

    transport.emit({
      type: 'event_batch',
      events: [event(1, 'server_ready')],
      store_id: 'store-3',
      history_after_sequence: 0,
      rebootstrap: true,
    });
    await waitForController(controller, state => state.iterations.revision === 'store-c');

    expect(
      transport.requests.filter(request => request.type === 'query.iterations').slice(-2),
    ).toEqual([
      {type: 'query.iterations', expected_store_id: 'store-2'},
      {type: 'query.iterations', expected_store_id: 'store-3'},
    ]);
    expect(controller.state.errorBanner).toBeNull();
    await controller.stop();
  });

  it.each([
    'success',
    'failure',
  ] as const)('retires a pending iteration %s after stop', async outcome => {
    const transport = new FakeTransport();
    const pending = deferred<ProtocolResponse>();
    if (outcome === 'success') {
      transport.iterationResponses.push(pending.promise);
    } else {
      transport.iterationResponses.push(
        iterationPageResponse('late', null, 0, 100, 'page-2'),
        pending.promise,
      );
    }
    const controller = new SocketSessionController(transport);

    const started = controller.start();
    await settleMicrotasks();
    await controller.stop();
    if (outcome === 'success') {
      pending.resolve(iterationPageResponse('late', null, 0, 1, null));
    } else {
      pending.reject(new ServerError('late iteration failure'));
    }
    await started;

    expect(controller.state.iterations.revision).toBeNull();
    expect(controller.state.errorBanner).toBeNull();
  });

  it('publishes more than one hundred iteration roots only after the last page', async () => {
    const transport = new FakeTransport();
    const lastPage = deferred<ProtocolResponse>();
    transport.iterationResponses.push(
      iterationPageResponse('bulk', null, 0, 100, 'page-2'),
      lastPage.promise,
    );
    const controller = new SocketSessionController(transport);

    const started = controller.start();
    await settleMicrotasks();
    expect(controller.state.iterations.revision).toBeNull();
    expect(iterationRailRounds(controller.state)).toEqual([]);

    lastPage.resolve(iterationPageResponse('bulk', 'page-2', 100, 25, null));
    await started;

    expect(controller.state.iterations.revision).toBe('bulk');
    expect(iterationRailRounds(controller.state)).toHaveLength(125);
    expect(transport.requests.filter(request => request.type === 'query.iterations')).toEqual([
      {type: 'query.iterations', expected_store_id: ''},
      {type: 'query.iterations', expected_store_id: '', cursor: 'page-2'},
    ]);
  });

  it('reports an initial second-page rejection without rejecting bootstrap or publishing a partial tree', async () => {
    const transport = new FakeTransport();
    transport.iterationResponses.push(
      iterationPageResponse('initial', null, 0, 100, 'page-2'),
      new ServerError('initial second iteration page failed'),
    );
    const controller = new SocketSessionController(transport);

    await expect(controller.start()).resolves.toBeUndefined();

    expect(controller.state.iterations.revision).toBeNull();
    expect(iterationRailRounds(controller.state)).toEqual([]);
    expect(controller.state.errorBanner).toMatchObject({
      scope: 'request',
      message: 'initial second iteration page failed',
    });
  });

  it('reports an initial repeated cursor without rejecting bootstrap or publishing a partial tree', async () => {
    const transport = new FakeTransport();
    transport.iterationResponses.push(
      iterationPageResponse('initial', null, 0, 100, 'page-2'),
      iterationPageResponse('initial', 'page-2', 100, 1, 'page-2'),
    );
    const controller = new SocketSessionController(transport);

    await expect(controller.start()).resolves.toBeUndefined();

    expect(controller.state.iterations.revision).toBeNull();
    expect(iterationRailRounds(controller.state)).toEqual([]);
    expect(controller.state.errorBanner).toMatchObject({
      scope: 'request',
      message: 'iteration query repeated a paging cursor',
    });
  });

  it('retains the last complete iteration tree when a later page fails', async () => {
    const transport = new FakeTransport();
    transport.iterationsReady = true;
    transport.iterations = iterationPage('complete', 0);
    const controller = new SocketSessionController(transport);
    await controller.start();
    transport.iterationResponses.push(
      iterationPageResponse('replacement', null, 0, 100, 'page-2'),
      new ServerError('second iteration page failed'),
    );
    const failed = waitForController(
      controller,
      state => state.errorBanner?.message === 'second iteration page failed',
    );

    transport.emit({
      type: 'event',
      event: {
        ...event(13, 'iterations_changed'),
        data: {kind: 'iterations_changed', revision: 'replacement'},
      },
    });
    await failed;

    expect(controller.state.iterations.revision).toBe('complete');
    expect(iterationRailRounds(controller.state).map(row => row.iterationId)).toEqual([
      'generation:0',
    ]);
  });

  it('retains the last complete iteration tree when paging repeats a cursor', async () => {
    const transport = new FakeTransport();
    transport.iterationsReady = true;
    transport.iterations = iterationPage('complete', 0);
    const controller = new SocketSessionController(transport);
    await controller.start();
    transport.iterationResponses.push(
      iterationPageResponse('replacement', null, 0, 100, 'page-2'),
      iterationPageResponse('replacement', 'page-2', 100, 1, 'page-2'),
    );
    const failed = waitForController(
      controller,
      state => state.errorBanner?.message === 'iteration query repeated a paging cursor',
    );

    transport.emit({
      type: 'event',
      event: {
        ...event(13, 'iterations_changed'),
        data: {kind: 'iterations_changed', revision: 'replacement'},
      },
    });
    await failed;

    expect(controller.state.iterations.revision).toBe('complete');
    expect(iterationRailRounds(controller.state).map(row => row.iterationId)).toEqual([
      'generation:0',
    ]);
  });

  it('falls back only when the initial iteration capability probe is rejected', async () => {
    const transport = new FakeTransport();
    transport.iterationResponses.push(new ServerError('Unsupported protocol request'));
    const controller = new SocketSessionController(transport);

    await controller.start();
    expect(controller.state.errorBanner).toBeNull();
    transport.emit({
      type: 'event',
      event: {
        ...event(13, 'iterations_changed'),
        data: {kind: 'iterations_changed', revision: 'ignored'},
      },
    });
    await settleMicrotasks();

    expect(transport.requests.filter(request => request.type === 'query.iterations')).toHaveLength(
      1,
    );
    expect(controller.state.errorBanner).toBeNull();
  });

  it('applies a replay batch before reconciling its active execution checkpoint', async () => {
    const transport = new FakeTransport();
    const controller = new SocketSessionController(transport);
    await controller.start();

    transport.emit({
      type: 'event_batch',
      events: [
        {
          sequence: 1,
          timestamp: '2026-01-01T00:00:00Z',
          type: 'agent_execution_started',
          execution_id: 'stale-execution',
          agent_kind: 'implementer',
          round_label: 'round-1-implementer',
          data: {
            kind: 'agent_execution_started',
            stage: 'implementation',
            attempt: 1,
            system_prompt: '',
            user_prompt: 'Implement the queue',
            activity: {
              kind: 'agent_execution_activity_changed',
              mode: 'thinking',
              summary: 'Inspecting the queue',
              tool: null,
            },
          },
        },
        event(2, 'agent_output_chunk', 'persisted output\n'),
      ],
      through_sequence: 2,
      active_executions: [],
    });

    expect(controller.state.core.sequence).toBe(2);
    expect(controller.state.core.transcript.at(-1)?.content).toBe('persisted output\n');
    expect(controller.state.core.activeExecutions).toEqual({});
  });

  it('keeps the last complete execution checkpoint when a fragmented rebootstrap disconnects', async () => {
    const transport = new FakeTransport();
    const controller = new SocketSessionController(transport, undefined, undefined, []);
    await controller.start();
    transport.emit({
      type: 'event',
      event: {
        ...event(1, 'agent_execution_started'),
        execution_id: 'previous',
        agent_kind: 'implementer',
        round_label: 'round-1-implementer',
        data: {
          kind: 'agent_execution_started',
          stage: 'implementation',
          attempt: 1,
          system_prompt: '',
          user_prompt: 'Keep this visible',
          activity: {
            kind: 'agent_execution_activity_changed',
            mode: 'thinking',
            summary: 'Working',
            tool: null,
          },
        },
      },
    });
    transport.emit({
      type: 'event',
      event: statusEvent(2, 'previous', 'agent_output_chunk', {progress: 'Still working'}),
    });
    const checkpointId = 'a'.repeat(64);
    transport.emit({
      type: 'event_batch',
      events: [event(1, 'server_ready')],
      through_sequence: 1,
      active_executions: null,
      active_executions_checkpoint_id: checkpointId,
      store_id: 'replacement-store',
      history_after_sequence: 0,
      rebootstrap: true,
    });
    expect(Object.keys(controller.state.core.activeExecutions)).toEqual(['previous']);
    transport.emit({
      type: 'active_executions_chunk',
      checkpoint_id: checkpointId,
      chunk_index: 0,
      chunk_count: 2,
      payload: '[',
      through_sequence: 1,
      store_id: 'replacement-store',
      history_after_sequence: 0,
    });

    transport.disconnect(new Error('Server event stream disconnected'));

    expect(Object.keys(controller.state.core.activeExecutions)).toEqual(['previous']);
    expect(controller.state.core.executionStatuses['previous']?.progress).toBe('Still working');
    expect(controller.state.eventStreamAvailable).toBe(false);
    await controller.stop();
  });

  it('publishes a completed fragmented rebootstrap atomically', async () => {
    const transport = new FakeTransport();
    const controller = new SocketSessionController(transport, undefined, undefined, []);
    await controller.start();
    transport.emit({
      type: 'event',
      event: {
        ...event(1, 'agent_execution_started'),
        execution_id: 'previous',
        agent_kind: 'implementer',
        round_label: 'round-0-implementer',
        data: {
          kind: 'agent_execution_started',
          stage: 'implementation',
          attempt: 1,
          system_prompt: '',
          user_prompt: 'Previous assignment',
          activity: {
            kind: 'agent_execution_activity_changed',
            mode: 'thinking',
            summary: 'Working',
            tool: null,
          },
        },
      },
    });
    transport.emit({
      type: 'event',
      event: statusEvent(2, 'previous', 'agent_output_chunk', {progress: 'Previous progress'}),
    });
    const checkpoint = [
      {
        execution_id: 'replacement',
        agent_kind: 'judge',
        round_label: 'round-1-judge',
        stage: 'judge',
        attempt: null,
        assignment: 'Review',
        started_at: '2026-01-01T00:00:02Z',
        activity: {
          kind: 'agent_execution_activity_changed' as const,
          mode: 'thinking' as const,
          summary: 'Reviewing',
          tool: null,
        },
        provider: null,
        model: null,
      },
    ];
    const payload = JSON.stringify(checkpoint);
    const checkpointId = createHash('sha256').update(payload, 'utf8').digest('hex');
    transport.emit({
      type: 'event_batch',
      events: [
        event(1, 'server_ready'),
        {
          ...event(2, 'agent_execution_started'),
          execution_id: 'replacement',
          agent_kind: 'judge',
          round_label: 'round-1-judge',
          data: {
            kind: 'agent_execution_started',
            stage: 'judge',
            attempt: null,
            system_prompt: '',
            user_prompt: 'Review',
            activity: {
              kind: 'agent_execution_activity_changed',
              mode: 'thinking',
              summary: 'Reviewing',
              tool: null,
            },
          },
        },
        statusEvent(3, 'replacement', 'agent_output_chunk', {progress: 'Reviewing output'}),
      ],
      through_sequence: 3,
      active_executions: null,
      active_executions_checkpoint_id: checkpointId,
      store_id: 'replacement-store',
      history_after_sequence: 0,
      rebootstrap: true,
    });

    expect(Object.keys(controller.state.core.activeExecutions)).toEqual(['previous']);
    expect(controller.state.core.executionStatuses['previous']?.progress).toBe('Previous progress');
    transport.emit({
      type: 'active_executions_chunk',
      checkpoint_id: checkpointId,
      chunk_index: 0,
      chunk_count: 1,
      payload,
      through_sequence: 3,
      store_id: 'replacement-store',
      history_after_sequence: 0,
    });

    expect(Object.keys(controller.state.core.activeExecutions)).toEqual(['replacement']);
    expect(controller.state.core.executionStatuses['replacement']?.progress).toBe(
      'Reviewing output',
    );
    await controller.stop();
  });

  it('does not surface an old failure banner when a replay batch resumes running', async () => {
    const transport = new FakeTransport();
    const controller = new SocketSessionController(transport);
    await controller.start();

    transport.emit({
      type: 'event_batch',
      events: [
        {
          ...event(1, 'run_failed'),
          diagnostic: {
            code: 'interrupted',
            summary: 'A previous process was interrupted.',
            scope: 'run',
            severity: 'fatal',
            retryability: 'never',
          },
        },
        {
          ...event(2, 'run_started'),
          data: {kind: 'run_started', outer_loop: 'agent', input: '.', max_rounds: 3},
        },
      ],
      through_sequence: 2,
      active_executions: [],
    });

    expect(controller.state.core.status).toBe('running');
    expect(controller.state.errorBanner).toBeNull();
  });

  it('shows the final failure from a terminal event batch', async () => {
    const transport = new FakeTransport();
    const controller = new SocketSessionController(transport);
    await controller.start();

    transport.emit({
      type: 'event_batch',
      events: [
        {
          ...event(1, 'run_failed'),
          diagnostic: {
            code: 'run_failed',
            summary: 'The current run failed.',
            scope: 'run',
            severity: 'fatal',
            retryability: 'never',
          },
        },
      ],
      through_sequence: 1,
      active_executions: [],
    });

    expect(controller.state.core.status).toBe('failed');
    expect(controller.state.errorBanner).toMatchObject({message: 'The current run failed.'});
  });

  it('keeps terminal state when the stream closes after completion', async () => {
    const transport = new FakeTransport();
    const controller = new SocketSessionController(transport);
    await controller.start();
    // The transport's snapshot declares sequence 12; completing at the same
    // cursor means the fold has reached every event that snapshot knew about.
    transport.emit({type: 'event', event: event(12, 'run_finished')});
    transport.disconnect(new Error('closed'));

    expect(controller.state.core.status).toBe('completed');
    expect(controller.state.overlay).toBeNull();
    expect(controller.state.errorBanner).toBeNull();
  });

  it('preserves backend execution state but suppresses live activity after a disconnect', async () => {
    const transport = new FakeTransport();
    // An empty backoff schedule: this test is about the disconnected state
    // itself, not the reconnect that would otherwise follow.
    const controller = new SocketSessionController(transport, undefined, undefined, []);
    await controller.start();
    transport.emit({
      type: 'event',
      event: {
        sequence: 1,
        timestamp: '2026-01-01T00:00:00Z',
        type: 'agent_execution_started',
        execution_id: 'impl-1',
        agent_kind: 'implementer',
        round_label: 'round-1-implementer',
        data: {
          kind: 'agent_execution_started',
          stage: 'implementation',
          attempt: 1,
          system_prompt: '',
          user_prompt: 'Implement the queue',
          activity: {
            kind: 'agent_execution_activity_changed',
            mode: 'thinking',
            summary: 'Inspecting the queue',
            tool: null,
          },
        },
      },
    });
    expect(controller.state.core.activeExecutions['impl-1']).toBeDefined();

    transport.disconnect(new Error('Server event stream disconnected'));

    expect(controller.state.core.activeExecutions['impl-1']).toBeDefined();
    expect(controller.state.eventStreamAvailable).toBe(false);
    expect(controller.state.errorBanner).toMatchObject({scope: 'transport'});
  });

  it('prefers structured response and protocol diagnostics over legacy messages', async () => {
    const diagnostic = {
      id: 'request-1',
      code: 'unknown_future_code',
      summary: 'The requested operation was rejected.',
      detail: 'PermissionError: missing capability.',
      hint: 'Request the required capability.',
      scope: 'request' as const,
      severity: 'error' as const,
      retryability: 'manual' as const,
    };
    const transport = new FakeTransport(
      [],
      [],
      undefined,
      new ServerError('legacy request message', diagnostic),
    );
    const controller = new SocketSessionController(transport);

    await controller.submitCommand('/resume');

    expect(controller.state.errorBanner).toMatchObject({
      message: diagnostic.summary,
      detail: diagnostic.detail,
      hint: diagnostic.hint,
      diagnosticId: diagnostic.id,
      scope: 'request',
    });

    const protocolTransport = new FakeTransport();
    const protocolController = new SocketSessionController(protocolTransport);
    await protocolController.start();
    protocolTransport.emit({
      type: 'protocol_error',
      code: 'unknown_protocol_code',
      message: 'legacy protocol message',
      diagnostic: {...diagnostic, id: 'protocol-1', scope: 'protocol', severity: 'warning'},
    });

    expect(protocolController.state.errorBanner).toMatchObject({
      message: diagnostic.summary,
      diagnosticId: 'protocol-1',
      scope: 'protocol',
      severity: 'recoverable',
    });
    protocolTransport.disconnect(new Error('Server event stream disconnected'));
    expect(protocolController.state.errorBanner).toMatchObject({
      message: diagnostic.summary,
      diagnosticId: 'protocol-1',
      scope: 'protocol',
    });
  });

  it('renders a performance curve from the perf command', async () => {
    const transport = new FakeTransport(
      [],
      [
        {
          round: 1,
          perf_metric: 1200,
          perf_unit: 'total_ops_per_sec',
          passed: true,
          profile_skipped: false,
        },
        {
          round: 2,
          perf_metric: 2400,
          perf_unit: 'total_ops_per_sec',
          passed: true,
          profile_skipped: false,
        },
      ],
    );
    const controller = new SocketSessionController(transport);

    await controller.submitCommand('/perf');

    expect(transport.requests).toEqual([{type: 'query.performance'}]);
    // The chart lands beside the transcript, not over it.
    expect(controller.state.overlay).toBeNull();
    expect(controller.state.layout.right?.view).toBe('perf');
    const pane = controller.state.layout.right;
    expect(pane === null ? null : rightPaneTitle(pane)).toBe('Performance');
    expect(paneContent(controller)).toContain('Performance · total_ops_per_sec');
    expect(paneContent(controller)).toContain('best r2 2.4k total_ops_per_sec');
    expect(controller.state.layout.focus).toBe('right');
  });

  // Post-#692 journals report a benchmark as a completed `gate_finished`
  // carrying the measurement, not as a `benchmark_result`, so the pane's
  // refresh trigger has to recognize both or the curve stays stale until an
  // unrelated event happens to reload it.
  it('reloads the perf pane when a benchmark arrives as a gate result', async () => {
    const transport = new FakeTransport();
    const controller = new SocketSessionController(transport);
    await controller.start();
    await controller.submitCommand('/perf');
    const loads = (): number => performanceQueries(transport);
    const before = loads();

    transport.emit({
      type: 'event',
      event: {
        sequence: 5,
        timestamp: '2026-01-01T00:00:05Z',
        type: 'gate_finished',
        status: 'completed',
        round_label: 'round-1',
        data: {kind: 'gate_finished', gate: 'benchmark', metric: 'ops', value: 9, unit: 'ops/s'},
      },
    });
    await Promise.resolve();
    await Promise.resolve();

    expect(loads()).toBe(before + 1);
  });

  it('opens a multi-turn chat panel and renders agent answers there', async () => {
    const transport = new FakeTransport(
      [
        chatEvent(
          1,
          'agent_output_chunk',
          {
            kind: 'agent_output_chunk',
            channel: 'analysis',
            content: 'Reading progress.md',
          },
          {invocation_id: 'chat-1'},
        ),
        chatEvent(
          2,
          'tool_call',
          {
            kind: 'tool_call',
            tool: 'read_file',
            args: {path: 'progress.md'},
            status: null,
          },
          {invocation_id: 'chat-1'},
        ),
        chatEvent(
          3,
          'chat',
          {
            kind: 'chat',
            answer: 'Round 2 improved throughput.',
          },
          {invocation_id: 'chat-1'},
        ),
      ],
      [],
      {
        question: 'what changed?',
        answer: 'Round 2 improved throughput.',
        effect: 'none',
      },
    );
    const controller = new SocketSessionController(transport);

    await controller.submitCommand('/chat');

    // Already on screen: /chat puts the pane keys on it instead of opening a
    // modal over the log.
    expect(controller.state.chatOpen).toBe(false);
    expect(controller.state.layout.focus).toBe('chat');
    expect(transport.requests).toEqual([]);

    await controller.sendChat('what changed?');

    expect(transport.requests).toEqual([{type: 'query.chat', text: 'what changed?'}]);
    // The exchange, and only the exchange: the chat agent's own narration and
    // tool turns belong in the transcript, not on top of the answer.
    expect(controller.state.chatConversation.map(entry => entry.kind)).toEqual([
      'user',
      'assistant',
    ]);
    expect(controller.state.chatConversation.at(-1)?.content).toBe('Round 2 improved throughput.');

    controller.closeChat();
    expect(controller.state.chatOpen).toBe(false);
    expect(controller.state.chatConversation).toHaveLength(2);
  });

  it('opens the chat as a modal where it cannot dock', async () => {
    const transport = new FakeTransport();
    const controller = new SocketSessionController(transport);
    // A terminal too narrow for two columns, reported by the renderer.
    controller.setChatDockFits(false);

    await controller.submitChat('what is happening?');

    expect(controller.state.chatOpen).toBe(true);
    expect(chatPaneVisible(controller.state)).toBe(false);
    expect(controller.state.chatConversation.at(-1)?.content).toBe('The implementer is running.');
  });

  it('keeps the log as the view when the chat opens over it', async () => {
    const transport = new FakeTransport();
    transport.experiments = [entry('H-01', 1, 1, {resolved_outcome: 'proven'})];
    const controller = new SocketSessionController(transport);
    await controller.start();
    // Too narrow to dock, so the question opens the modal.
    controller.setChatDockFits(false);

    await controller.sendChat('what is happening?');

    expect(controller.state.chatOpen).toBe(true);
    // The modal floats over the table. It must not put the operator into the
    // per-round transcript they never asked for.
    expect(experimentLogVisible(controller.state)).toBe(true);
    expect(controller.state.hypothesisScope).toBeNull();
    expect(controller.state.experimentLog?.entries).toHaveLength(1);
  });

  it('offers /chat in the palette only where the chat is not already on screen', async () => {
    const transport = new FakeTransport();
    transport.experiments = [
      entry('H-01', 1, 1, {rounds: [{round: 1, passed: true, reviewed: true}]}),
    ];
    const controller = new SocketSessionController(transport);
    await controller.start();

    await controller.submitCommand('/help');
    expect(paletteNames(controller)).not.toContain('/chat');

    // Inside a hypothesis the chat is a dialog again, so the command returns.
    controller.enterExperimentDrilldown();
    await controller.submitCommand('/help');
    expect(paletteNames(controller)).toContain('/chat');
  });

  it('carries the modal conversation back into the docked pane', async () => {
    const transport = new FakeTransport();
    transport.experiments = [
      entry('H-01', 1, 1, {rounds: [{round: 1, passed: true, reviewed: true}]}),
    ];
    const controller = new SocketSessionController(transport);
    await controller.start();
    controller.enterExperimentDrilldown();

    // Asked from the trajectory view, where the chat is a pop-up.
    await controller.submitCommand('/chat why did r1 fail?');
    expect(controller.state.chatOpen).toBe(true);

    controller.live();

    // Back on the landing view the same conversation is in the column, both
    // the question and what came back.
    expect(chatPaneVisible(controller.state)).toBe(true);
    expect(controller.state.chatConversation.map(entry => entry.content)).toEqual([
      'why did r1 fail?',
      'The implementer is running.',
    ]);
  });

  it('opens the chat as a modal inside a hypothesis', async () => {
    const transport = new FakeTransport();
    transport.experiments = [
      entry('H-01', 1, 1, {rounds: [{round: 1, passed: true, reviewed: true}]}),
    ];
    const controller = new SocketSessionController(transport);
    await controller.start();
    controller.enterExperimentDrilldown();

    await controller.sendChat('why did r1 fail?');

    // The row belongs to the transcript here, so the chat is the dialog it was.
    expect(controller.state.chatOpen).toBe(true);

    // Back on the landing view it docks again, transcript intact.
    controller.live();
    expect(controller.state.chatOpen).toBe(false);
    expect(chatPaneVisible(controller.state)).toBe(true);
    expect(controller.state.chatConversation.at(0)?.content).toBe('why did r1 fail?');
  });

  it('opens chat and sends an initial message from the command line', async () => {
    const transport = new FakeTransport([], [], {
      question: 'why?',
      answer: 'Because the configuration failed.',
      effect: 'none',
    });
    const controller = new SocketSessionController(transport);

    await controller.submitCommand('/chat why?');

    expect(controller.state.chatOpen).toBe(false);
    expect(controller.state.layout.focus).toBe('chat');
    expect(transport.requests).toEqual([{type: 'query.chat', text: 'why?'}]);
  });

  it('batches messages queued while the chat agent is still working', async () => {
    const transport = new DeferredChatTransport();
    const controller = new SocketSessionController(transport);

    const first = controller.sendChat('first question');
    const second = controller.sendChat('follow-up question');
    const third = controller.sendChat('one more detail');

    expect(transport.requests).toEqual([{type: 'query.chat', text: 'first question'}]);
    expect(controller.state.chatConversation).toMatchObject([
      {kind: 'user', labelKind: 'user', content: 'first question'},
      {kind: 'user', labelKind: 'user_queued', content: 'follow-up question'},
      {kind: 'user', labelKind: 'user_queued', content: 'one more detail'},
    ]);

    transport.resolveNext('first answer');
    await Promise.resolve();
    await Promise.resolve();

    expect(transport.requests).toEqual([
      {type: 'query.chat', text: 'first question'},
      {type: 'query.chat', text: 'follow-up question\n\none more detail'},
    ]);
    expect(controller.state.chatConversation[1]?.labelKind).toBe('user');
    expect(controller.state.chatConversation[2]?.labelKind).toBe('user');

    transport.resolveNext('follow-up answer');
    await Promise.all([first, second, third]);

    expect(controller.state.chatPending).toBe(false);
    expect(controller.state.chatConversation.map(entry => entry.content)).toEqual([
      'first question',
      'follow-up question',
      'one more detail',
      'first answer',
      'follow-up answer',
    ]);
  });

  it('starts a new batch for messages entered after a queued batch is sent', async () => {
    const transport = new DeferredChatTransport();
    const controller = new SocketSessionController(transport);

    const first = controller.sendChat('first');
    const second = controller.sendChat('second');
    const third = controller.sendChat('third');
    transport.resolveNext('first answer');
    await Promise.resolve();
    await Promise.resolve();

    expect(transport.requests.at(-1)).toEqual({type: 'query.chat', text: 'second\n\nthird'});

    const fourth = controller.sendChat('fourth');
    transport.resolveNext('batched answer');
    await Promise.resolve();
    await Promise.resolve();

    expect(transport.requests.at(-1)).toEqual({type: 'query.chat', text: 'fourth'});

    transport.resolveNext('fourth answer');
    await Promise.all([first, second, third, fourth]);
  });

  it('shows chat request failures as explicit failed trajectory entries', async () => {
    const transport = new FakeTransport([], [], undefined, new Error('Codex exited with code 1'));
    const controller = new SocketSessionController(transport);

    await controller.submitCommand('/chat');
    await controller.sendChat('what happened?');

    expect(controller.state.chatPending).toBe(false);
    expect(controller.state.chatConversation.at(-1)).toMatchObject({
      kind: 'result',
      labelKind: 'chat_failed',
      tone: 'failure',
      content: 'Codex exited with code 1',
    });
  });

  it('starts on the requested theme and defaults to dark', () => {
    expect(new SocketSessionController(new FakeTransport()).state.themeName).toBe('dark');
    expect(
      new SocketSessionController(new FakeTransport(), 'catppuccin-latte').state.themeName,
    ).toBe('catppuccin-latte');
  });

  it('opens the theme list as a selection starting on the active theme', async () => {
    const transport = new FakeTransport();
    const controller = new SocketSessionController(transport, 'solarized-dark');

    await controller.submitCommand('/theme');

    expect(controller.state.themePicker?.selected).toBe('solarized-dark');
    // The list is a selection, not a text overlay.
    expect(controller.state.overlay).toBeNull();
    expect(controller.state.themeName).toBe('solarized-dark');
    expect(transport.requests).toEqual([]);
  });

  it('applies the selected theme and closes the picker', async () => {
    const transport = new FakeTransport();
    const controller = new SocketSessionController(transport);

    await controller.submitCommand('/theme');
    controller.moveThemeSelection(2);
    controller.applySelectedTheme();

    expect(controller.state.themeName).toBe('solarized-dark');
    expect(controller.state.themePicker).toBeNull();
    expect(transport.requests).toEqual([]);
  });

  it('closes the picker without switching when it is dismissed', async () => {
    const controller = new SocketSessionController(new FakeTransport(), 'light');

    await controller.submitCommand('/theme');
    controller.moveThemeSelection(1);
    controller.closeThemePicker();

    expect(controller.state.themeName).toBe('light');
    expect(controller.state.themePicker).toBeNull();
  });

  it('closes the picker when the selection is the theme already in use', async () => {
    const controller = new SocketSessionController(new FakeTransport(), 'light');

    await controller.submitCommand('/theme');
    controller.applySelectedTheme();

    expect(controller.state.themeName).toBe('light');
    expect(controller.state.themePicker).toBeNull();
  });

  it('switches theme locally and closes the picker', async () => {
    const transport = new FakeTransport();
    const controller = new SocketSessionController(transport);

    await controller.submitCommand('/theme');
    await controller.submitCommand('/theme high-contrast-dark');

    expect(controller.state.themeName).toBe('high-contrast-dark');
    expect(controller.state.themePicker).toBeNull();
    expect(controller.state.overlay).toBeNull();
    expect(transport.requests).toEqual([]);
  });

  it('makes the experiment log the landing view without a command', async () => {
    const transport = new FakeTransport();
    transport.experiments = [entry('H-01', 1, 1, {resolved_outcome: 'proven'})];
    const controller = new SocketSessionController(transport);

    await controller.start();

    expect(transport.requests).toContainEqual({type: 'query.experiments'});
    expect(controller.state.experimentLog?.entries).toHaveLength(1);
    expect(controller.state.experimentLog?.selectedId).toBe('H-01');
    expect(controller.state.overlay).toBeNull();
  });

  it('rejects the removed experiment-log commands without reaching the backend', async () => {
    const transport = new FakeTransport();
    const controller = new SocketSessionController(transport);

    await controller.submitCommand('/history');
    expect(controller.state.errorBanner).toBeNull();
    expect(controller.state.inputError).toContain('Unknown command /history');

    await controller.submitCommand('/history rounds');
    expect(controller.state.inputError).toContain('Unknown command /history rounds');

    await controller.submitCommand('/experiments');
    expect(controller.state.inputError).toContain('Unknown command /experiments');

    controller.clearInputError();
    expect(controller.state.inputError).toBeNull();

    await controller.submitCommand('/history');
    expect(controller.state.inputError).toContain('Unknown command /history');

    expect(transport.requests).toEqual([]);
  });

  it('returns to the experiment log from a hypothesis without a command', async () => {
    const transport = new FakeTransport();
    transport.experiments = [
      entry('H-01', 1, 1, {rounds: [{round: 1, passed: true, reviewed: true}]}),
    ];
    const controller = new SocketSessionController(transport);
    await controller.start();
    controller.enterExperimentDrilldown();
    expect(controller.state.hypothesisDetail).not.toBeNull();
    controller.enterExperimentDrilldown();
    expect(controller.state.hypothesisScope).not.toBeNull();

    // What Ctrl+L and Escape are bound to.
    controller.live();

    expect(controller.state.hypothesisScope).toBeNull();
    expect(controller.state.experimentLog?.entries).toHaveLength(1);
  });

  it('refetches the log when experiments change and keeps the selected row', async () => {
    const transport = new FakeTransport();
    transport.experiments = [
      entry('H-01', 1, 1, {resolved_outcome: 'proven'}),
      entry('H-02', 2, 2, {active: true}),
    ];
    const controller = new SocketSessionController(transport);
    await controller.start();
    controller.moveExperimentSelection(1);
    expect(controller.state.experimentLog?.selectedId).toBe('H-02');
    const before = transport.requests.length;

    // The active hypothesis resolves and a new one opens above nothing.
    transport.experiments = [
      entry('H-01', 1, 1, {resolved_outcome: 'proven'}),
      entry('H-02', 2, 3, {resolved_outcome: 'rejected'}),
      entry('H-03', 4, 4, {active: true}),
    ];
    transport.emit({type: 'event', event: event(9, 'experiments_changed')});
    await Promise.resolve();
    await Promise.resolve();

    const refetches = transport.requests.slice(before).filter(r => r.type === 'query.experiments');
    expect(refetches).toHaveLength(1);
    expect(controller.state.experimentLog?.entries).toHaveLength(3);
    expect(controller.state.experimentLog?.selectedId).toBe('H-02');
    expect(controller.state.experimentLog?.entries[1]?.resolved_outcome).toBe('rejected');
  });

  it('applies a revisioned replacement without dropping unchanged hypotheses', async () => {
    const transport = new RevisionedExperimentsTransport([
      entry('H-01', 1, 1, {resolved_outcome: 'proven'}),
      entry('H-02', 2, 2, {active: true}),
    ]);
    const controller = new SocketSessionController(transport);
    await controller.start();

    transport.emitExperimentChange(2, 2);
    expect(transport.experimentInputs.at(-1)).toEqual({
      type: 'query.experiments',
      after: {run_id: 'run', projection_id: 'projection', revision: 1},
    });
    transport.resolveExperiment([entry('H-02', 2, 3, {resolved_outcome: 'rejected'})], {
      run_id: 'run',
      projection_id: 'projection',
      from_revision: 1,
      through_revision: 2,
      reset: false,
    });
    await new Promise<void>(resolve => setTimeout(resolve, 0));

    expect(controller.state.experimentLog?.entries.map(item => item.hypothesis_id)).toEqual([
      'H-01',
      'H-02',
    ]);
    expect(controller.state.experimentLog?.entries[1]?.resolved_outcome).toBe('rejected');
  });

  it('recovers from a delta whose base does not match the applied cursor', async () => {
    const transport = new RevisionedExperimentsTransport([entry('H-old', 1, 1, {})]);
    const controller = new SocketSessionController(transport);
    await controller.start();

    transport.emitExperimentChange(2, 2);
    transport.resolveExperiment([entry('H-wrong', 2, 2, {})], {
      run_id: 'run',
      projection_id: 'projection',
      from_revision: 0,
      through_revision: 2,
      reset: false,
    });
    await new Promise<void>(resolve => setTimeout(resolve, 0));

    expect(transport.experimentInputs.at(-1)).toEqual({type: 'query.experiments'});
    transport.resolveExperiment([entry('H-current', 1, 2, {active: true})], {
      run_id: 'run',
      projection_id: 'projection',
      through_revision: 2,
      reset: true,
    });
    await new Promise<void>(resolve => setTimeout(resolve, 0));

    expect(controller.state.experimentLog?.entries.map(item => item.hypothesis_id)).toEqual([
      'H-current',
    ]);
  });

  it('converges through a burst that advances while a delta is in flight', async () => {
    const transport = new RevisionedExperimentsTransport([entry('H-01', 1, 1, {})]);
    const controller = new SocketSessionController(transport);
    await controller.start();

    transport.emitExperimentChange(2, 2);
    transport.emitExperimentChange(3, 3);
    transport.resolveExperiment([entry('H-01', 1, 2, {})], {
      run_id: 'run',
      projection_id: 'projection',
      from_revision: 1,
      through_revision: 2,
      reset: false,
    });
    await new Promise<void>(resolve => setTimeout(resolve, 0));

    expect(transport.experimentInputs.at(-1)).toEqual({
      type: 'query.experiments',
      after: {run_id: 'run', projection_id: 'projection', revision: 2},
    });
    transport.resolveExperiment([entry('H-01', 1, 3, {resolved_outcome: 'proven'})], {
      run_id: 'run',
      projection_id: 'projection',
      from_revision: 2,
      through_revision: 3,
      reset: false,
    });
    await new Promise<void>(resolve => setTimeout(resolve, 0));

    expect(controller.state.experimentLog?.entries[0]?.last_round).toBe(3);
    expect(transport.experimentInputs).toHaveLength(3);
  });

  it('rejects an in-flight response when the same run id is attached from another project', async () => {
    const transport = new RevisionedExperimentsTransport([entry('H-old', 1, 1, {})]);
    const controller = new SocketSessionController(transport);
    await controller.start();

    transport.emitExperimentChange(2, 2);
    transport.emitProjectAttached(3);
    transport.resolveExperiment([entry('H-stale', 1, 2, {})], {
      run_id: 'run',
      projection_id: 'old-project',
      through_revision: 2,
      reset: true,
    });
    await new Promise<void>(resolve => setTimeout(resolve, 0));

    expect(controller.state.experimentLog?.entries[0]?.hypothesis_id).toBe('H-old');
    expect(transport.experimentInputs).toHaveLength(3);
    transport.resolveExperiment([entry('H-new', 1, 1, {active: true})], {
      run_id: 'run',
      projection_id: 'new-project',
      through_revision: 1,
      reset: true,
    });
    await new Promise<void>(resolve => setTimeout(resolve, 0));

    expect(controller.state.experimentLog?.entries[0]?.hypothesis_id).toBe('H-new');
  });

  it('does not refetch the log for events that cannot change it', async () => {
    const transport = new FakeTransport();
    const controller = new SocketSessionController(transport);
    await controller.start();
    const before = transport.requests.length;

    transport.emit({type: 'event', event: event(1, 'agent_output_chunk', 'noise\n')});
    transport.emit({type: 'event', event: event(2, 'tool_call')});
    transport.emit({type: 'event', event: event(3, 'phase_finished')});
    transport.emit({type: 'event', event: event(4, 'round_finished')});
    await Promise.resolve();

    expect(transport.requests).toHaveLength(before);
  });

  it('keeps the log as the root view, with no way to dismiss it', async () => {
    const transport = new FakeTransport();
    transport.experiments = [
      entry('H-01', 1, 2, {
        resolved_outcome: 'proven',
        rounds: [
          {round: 1, passed: true, reviewed: true},
          {round: 2, passed: true, reviewed: true},
        ],
      }),
    ];
    const controller = new SocketSessionController(transport);
    await controller.start();

    // Per-round output is reachable only by opening a hypothesis.
    controller.enterExperimentDrilldown();
    expect(controller.state.hypothesisDetail).not.toBeNull();
    controller.enterExperimentDrilldown();
    expect(controller.state.hypothesisScope).not.toBeNull();

    // live() is the Ctrl+L path; it returns to the table rather than to an
    // unfiltered transcript.
    controller.live();
    expect(controller.state.hypothesisScope).toBeNull();
    expect(controller.state.experimentLog?.selectedId).toBe('H-01');
  });

  it('opens a hypothesis trajectory and returns with the selection intact', async () => {
    const transport = new FakeTransport();
    transport.experiments = [
      entry('H-01', 1, 1, {
        resolved_outcome: 'proven',
        rounds: [{round: 1, passed: true, reviewed: true}],
      }),
      entry('H-02', 2, 3, {
        resolved_outcome: 'rejected',
        rounds: [
          {round: 2, passed: false, reviewed: false},
          {round: 3, passed: false, reviewed: true},
        ],
      }),
    ];
    const controller = new SocketSessionController(transport);
    await controller.openExperimentLog();
    controller.moveExperimentSelection(1);

    controller.enterExperimentDrilldown();
    expect(controller.state.hypothesisDetail).toEqual({entryKey: 'H-02', selectedRound: 3});
    expect(controller.state.hypothesisScope).toBeNull();

    controller.enterExperimentDrilldown();
    expect(controller.state.hypothesisScope).toMatchObject({id: 'H-02', rounds: [2, 3]});
    expect(controller.state.hypothesisScope?.label).toBe('H-02 · r2-3');

    controller.leaveExperimentDrilldown();
    expect(controller.state.hypothesisScope).toBeNull();
    expect(controller.state.hypothesisDetail).toEqual({entryKey: 'H-02', selectedRound: 3});
    expect(controller.state.experimentLog?.selectedId).toBe('H-02');
  });

  it('loads the log before the first frame so it can be the landing view', async () => {
    const transport = new FakeTransport();
    transport.experiments = [entry('H-01', 1, 1, {resolved_outcome: 'proven'})];
    const controller = new SocketSessionController(transport);

    expect(controller.state.experimentLog?.pending).toBe(true);
    await controller.start();

    expect(transport.requests).toEqual([
      {type: 'query.snapshot'},
      {type: 'query.iterations', expected_store_id: ''},
      {type: 'query.experiments'},
      {type: 'query.design'},
    ]);
    expect(controller.state.experimentLog?.pending).toBe(false);
    expect(controller.state.experimentLog?.selectedId).toBe('H-01');
  });

  it('loads the design log with the experiments so the drill-down can annotate rounds', async () => {
    const transport = new FakeTransport();
    transport.design = [{round: 1, files: [{path: 'src/ring.rs', change: 'added'}]}];
    const controller = new SocketSessionController(transport);

    await controller.start();
    await new Promise<void>(resolve => setTimeout(resolve, 0));

    expect(controller.state.designLog).toEqual(transport.design);
  });

  it('leaves the design log unloaded until the backend reports it ready', async () => {
    const transport = new FakeTransport();
    transport.designReady = false;
    const controller = new SocketSessionController(transport);

    await controller.start();
    await new Promise<void>(resolve => setTimeout(resolve, 0));

    expect(controller.state.designLog).toBeNull();
  });

  it('renders /design in the right pane, joining stage facts by round', async () => {
    const transport = new FakeTransport();
    transport.experiments = [
      entry('H-01', 1, 1, {
        title: 'Pad the ring indices',
        rounds: [{round: 1, passed: true, reviewed: true, judge_verdict: 'pass'}],
      }),
    ];
    transport.design = [
      {
        round: 1,
        files: [
          {path: 'src/ring.rs', change: 'added'},
          {path: 'src/lib.rs', change: 'modified'},
        ],
      },
    ];
    const controller = new SocketSessionController(transport);
    await controller.start();
    await new Promise<void>(resolve => setTimeout(resolve, 0));

    await controller.submitCommand('/design');

    expect(controller.state.overlay).toBeNull();
    expect(controller.state.layout.right?.view).toBe('design');
    const pane = controller.state.layout.right;
    expect(pane === null ? null : rightPaneTitle(pane)).toBe('Design changes');
    expect(paneContent(controller)).toContain('Design changes by round');
    expect(paneContent(controller)).toContain('Round 1 · H-01 · Pad the ring indices');
    expect(paneContent(controller)).toContain('src/ring.rs, src/lib.rs');
    expect(controller.state.designLog).toEqual(transport.design);
  });

  it('says the design log is not ready instead of showing an empty pane', async () => {
    const transport = new FakeTransport();
    transport.designReady = false;
    const controller = new SocketSessionController(transport);

    await controller.submitCommand('/design');

    expect(paneContent(controller)).toContain('not available until a run is attached');
    expect(controller.state.designLog).toBeNull();
  });

  it('opens the newest diffable round and fetches only the file on screen', async () => {
    const transport = new FakeTransport();
    transport.design = [
      {
        round: 1,
        base: 'aaa1111',
        commit: 'bbb2222',
        files: [
          {path: 'src/ring.rs', change: 'modified'},
          {path: 'src/lib.rs', change: 'modified'},
        ],
      },
      // Newer but not diffable: the opener walks back to round 1.
      {round: 2, base: 'bbb2222', commit: 'ccc3333', files: []},
    ];
    transport.designPatchText = '@@ -1 +1 @@\n-old\n+new\n';
    const controller = new SocketSessionController(transport);
    await controller.start();
    await new Promise<void>(resolve => setTimeout(resolve, 0));

    controller.openRoundDiff();
    await new Promise<void>(resolve => setTimeout(resolve, 0));

    expect(controller.state.diffViewer).toMatchObject({
      round: 1,
      base: 'aaa1111',
      head: 'bbb2222',
      index: 0,
    });
    // Lazy per file: opening asked for the visible file only.
    expect(patchRequests(transport)).toEqual([
      {type: 'query.design_patch', base: 'aaa1111', head: 'bbb2222', path: 'src/ring.rs'},
    ]);
    expect(controller.state.diffViewer?.patches['src/ring.rs']).toEqual({
      kind: 'loaded',
      patch: '@@ -1 +1 @@\n-old\n+new\n',
      truncated: false,
    });

    // The next file costs one query; returning to a visited file costs none.
    controller.moveDiffFile(1);
    await new Promise<void>(resolve => setTimeout(resolve, 0));
    controller.moveDiffFile(-1);
    await new Promise<void>(resolve => setTimeout(resolve, 0));
    expect(patchRequests(transport)).toHaveLength(2);
    expect(patchRequests(transport).at(-1)).toMatchObject({path: 'src/lib.rs'});
  });

  it("diffs the drill-down's selected round rather than the newest one", async () => {
    const transport = new FakeTransport();
    transport.experiments = [
      entry('H-01', 1, 2, {
        rounds: [
          {round: 1, passed: true, reviewed: true},
          {round: 2, passed: true, reviewed: true},
        ],
      }),
    ];
    transport.design = [
      {round: 1, base: 'aaa1111', commit: 'bbb2222', files: [{path: 'src/a.rs', change: 'added'}]},
      {round: 2, base: 'bbb2222', commit: 'ccc3333', files: [{path: 'src/b.rs', change: 'added'}]},
    ];
    transport.designPatchText = '@@ -0,0 +1 @@\n+fn main() {}\n';
    const controller = new SocketSessionController(transport);
    await controller.start();
    await new Promise<void>(resolve => setTimeout(resolve, 0));

    controller.enterExperimentDrilldown();
    controller.moveHypothesisRoundSelection(-1);
    expect(controller.state.hypothesisDetail?.selectedRound).toBe(1);

    controller.openRoundDiff();
    expect(controller.state.diffViewer).toMatchObject({round: 1, base: 'aaa1111'});
    // The viewer replaced no overlay here, and closing restores the detail
    // view untouched underneath.
    controller.closeDiffViewer();
    expect(controller.state.diffViewer).toBeNull();
    expect(controller.state.hypothesisDetail?.selectedRound).toBe(1);
  });

  it('parks a failed patch query on the file, never the shared banner', async () => {
    const transport = new FakeTransport();
    transport.design = [
      {round: 1, base: 'aaa1111', commit: 'bbb2222', files: [{path: 'src/a.rs', change: 'added'}]},
    ];
    transport.designPatchError = new ServerError('base does not name a commit');
    const controller = new SocketSessionController(transport);
    await controller.start();
    await new Promise<void>(resolve => setTimeout(resolve, 0));

    controller.openRoundDiff();
    await new Promise<void>(resolve => setTimeout(resolve, 0));

    expect(controller.state.diffViewer?.patches['src/a.rs']).toEqual({
      kind: 'error',
      message: 'base does not name a commit',
    });
    expect(controller.state.errorBanner).toBeNull();
  });

  it('explains an undiffable request in the detail overlay instead of opening', async () => {
    const transport = new FakeTransport();
    transport.designReady = false;
    const controller = new SocketSessionController(transport);
    await controller.start();

    controller.openRoundDiff();
    expect(controller.state.diffViewer).toBeNull();
    expect(controller.state.overlay?.content).toContain('No design rounds have loaded yet');

    // With a log whose rounds recorded no changes, the wording is per round.
    transport.design = [{round: 1, base: 'aaa1111', commit: 'bbb2222', files: []}];
    transport.designReady = true;
    await controller.submitCommand('/design');
    controller.openRoundDiff(1);
    expect(controller.state.diffViewer).toBeNull();
    expect(controller.state.overlay?.content).toBe('Round 1 changed no workspace files.');
    controller.openRoundDiff(9);
    expect(controller.state.overlay?.content).toBe('Round 9 has no recorded design changes.');
  });

  it('keeps bootstrap pending until attached experiments become ready', async () => {
    const transport = new FakeTransport();
    transport.experimentsReady = false;
    const controller = new SocketSessionController(transport);
    await controller.start();

    expect(controller.state.experimentLog?.pending).toBe(true);
    expect(controller.state.experimentLog?.entries).toEqual([]);

    transport.experiments = [entry('H-resumed', 1, 1, {active: true})];
    transport.experimentsReady = true;
    transport.emit({type: 'event', event: event(1, 'experiments_changed')});
    await Promise.resolve();
    await Promise.resolve();

    expect(controller.state.experimentLog?.pending).toBe(false);
    expect(controller.state.experimentLog?.selectedId).toBe('H-resumed');
  });

  it('reports how long the landing view waited for experiments', async () => {
    const transport = new FakeTransport();
    transport.experiments = [entry('H-01', 1, 1, {resolved_outcome: 'proven'})];
    const traced: string[] = [];
    const controller = new SocketSessionController(transport, undefined, line => traced.push(line));

    await controller.start();

    expect(traced).toHaveLength(1);
    expect(traced[0]).toMatch(/^experiments loaded in \d+ms \(1 entries\)$/);
  });

  it('times the whole wait across a closed gate, and reports it once', async () => {
    const transport = new FakeTransport();
    transport.experimentsReady = false;
    const traced: string[] = [];
    const controller = new SocketSessionController(transport, undefined, line => traced.push(line));

    await controller.start();
    expect(traced).toEqual([]);

    transport.experiments = [entry('H-resumed', 1, 1, {active: true}), entry('H-02', 2, 2, {})];
    transport.experimentsReady = true;
    transport.emit({type: 'event', event: event(1, 'experiments_changed')});
    await Promise.resolve();
    await Promise.resolve();

    expect(traced).toEqual([expect.stringMatching(/^experiments loaded in \d+ms \(2 entries\)$/)]);

    // A later refresh is not a boot cost, so it does not report again.
    transport.emit({type: 'event', event: event(2, 'experiments_changed')});
    await Promise.resolve();
    await Promise.resolve();

    expect(traced).toHaveLength(1);
  });

  it('stays silent through the real sink unless the boot trace is switched on', async () => {
    const transport = new FakeTransport();
    transport.experiments = [entry('H-01', 1, 1, {resolved_outcome: 'proven'})];
    const written: string[] = [];
    const controller = new SocketSessionController(
      transport,
      undefined,
      resolveStartupTrace({VIBESYS_LAUNCH_START_MS: String(Date.now() - 25)}, line =>
        written.push(line),
      ),
    );

    await controller.start();

    expect(written).toEqual([]);
  });

  it('writes one anchored line through the real sink when the boot trace is on', async () => {
    const transport = new FakeTransport();
    transport.experiments = [entry('H-01', 1, 1, {resolved_outcome: 'proven'})];
    const written: string[] = [];
    const controller = new SocketSessionController(
      transport,
      undefined,
      resolveStartupTrace(
        {VIBESYS_BOOT_TRACE: '1', VIBESYS_LAUNCH_START_MS: String(Date.now() - 25)},
        line => written.push(line),
      ),
    );

    await controller.start();

    expect(written).toHaveLength(1);
    expect(written[0]).toMatch(/^experiments loaded in \d+ms \(1 entries\); \d+ms since launch$/);
  });

  it('omits the since-launch suffix when the launch anchor is absent', async () => {
    const transport = new FakeTransport();
    transport.experiments = [entry('H-01', 1, 1, {resolved_outcome: 'proven'})];
    const written: string[] = [];
    const controller = new SocketSessionController(
      transport,
      undefined,
      resolveStartupTrace({VIBESYS_BOOT_TRACE: '1'}, line => written.push(line)),
    );

    await controller.start();

    expect(written).toHaveLength(1);
    expect(written[0]).toMatch(/^experiments loaded in \d+ms \(1 entries\)$/);
    expect(written[0]).not.toContain('since launch');
  });

  it('coalesces refetches when a burst of experiment changes lands', async () => {
    const transport = new FakeTransport();
    const controller = new SocketSessionController(transport);
    await controller.start();
    const before = transport.requests.length;

    transport.emit({
      type: 'event_batch',
      events: [event(1, 'experiments_changed'), event(2, 'experiments_changed')],
    });
    await Promise.resolve();
    await Promise.resolve();

    const refetches = transport.requests.slice(before).filter(r => r.type === 'query.experiments');
    expect(refetches).toHaveLength(1);
  });

  it('refetches again when experiments change during an in-flight fetch', async () => {
    const transport = new DeferredExperimentsTransport();
    const controller = new SocketSessionController(transport);
    await controller.start();

    transport.emit(event(1, 'experiments_changed'));
    expect(transport.experimentRequests).toBe(2);

    transport.emit(event(2, 'experiments_changed'));
    transport.emit(event(3, 'experiments_changed'));
    transport.resolveExperiment([entry('H-stale', 1, 1, {active: true})]);
    // A macrotask, because the design refresh sits between the answer and the
    // queued refetch and its length is not this test's concern.
    await new Promise<void>(resolve => setTimeout(resolve, 0));

    expect(transport.experimentRequests).toBe(3);
    transport.resolveExperiment([entry('H-current', 1, 2, {resolved_outcome: 'proven'})]);
    await new Promise<void>(resolve => setTimeout(resolve, 0));

    expect(controller.state.experimentLog?.selectedId).toBe('H-current');
    expect(transport.experimentRequests).toBe(3);
  });

  for (const staleOutcome of ['success', 'failure'] as const) {
    it(`follows a ${staleOutcome} stale design request with one current refresh`, async () => {
      const transport = new InvalidatedDesignTransport();
      const controller = new SocketSessionController(transport);
      await controller.start();
      expect(transport.designRequests).toBe(1);

      transport.design = [
        {
          round: 1,
          base: 'aaa1111',
          commit: 'bbb2222',
          files: [{path: 'src/queue.rs', change: 'modified'}],
        },
      ];
      transport.emitExperimentChange(1);
      transport.emitExperimentChange(2);
      transport.emitExperimentChange(3);
      expect(transport.experimentRequests).toBe(2);

      // The first invalidation's experiment request finishes while the old
      // design query is still held. The burst asks for only one more
      // experiment request, and every resulting design refresh coalesces.
      await transport.resolveExperiments();
      await settlePromiseEffects();
      expect(transport.experimentRequests).toBe(3);
      await transport.resolveExperiments();
      await settlePromiseEffects();

      await transport.settleDesign(staleOutcome);
      await settlePromiseEffects();
      expect(transport.designRequests).toBe(2);

      await transport.settleDesign('success');
      await settlePromiseEffects();
      expect(controller.state.designLog?.map(round => round.round)).toEqual([1]);
      expect(transport.designRequests).toBe(2);
      await controller.stop();
    });
  }

  it('retires a queued design refresh when the controller stops', async () => {
    const transport = new InvalidatedDesignTransport();
    const controller = new SocketSessionController(transport);
    await controller.start();
    transport.emitExperimentChange(1);
    await transport.resolveExperiments();
    await settlePromiseEffects();

    await controller.stop();
    await transport.settleDesign('success');
    await settlePromiseEffects();

    expect(transport.designRequests).toBe(1);
  });

  it('orders a newer supplemental design refresh after an older pane request', async () => {
    const transport = new CrossOwnedDesignTransport();
    const controller = new SocketSessionController(transport);
    await controller.start();
    await transport.settleDesign('success');

    transport.design = [designRound(1)];
    const pane = controller.openPane('design');
    transport.design = [designRound(1), designRound(2)];
    transport.emitExperimentChange(1);
    await settlePromiseEffects();

    // The invalidation is retained behind the older pane request instead of
    // starting another owner that can publish before it.
    expect(transport.designRequests).toBe(2);
    await transport.settleDesign('success');
    await settlePromiseEffects();
    expect(transport.designRequests).toBe(3);
    expect(controller.state.layout.right).toMatchObject({view: 'design', pending: true});

    await transport.settleDesign('success');
    await pane;
    await settlePromiseEffects();
    expect(controller.state.designLog?.map(round => round.round)).toEqual([1, 2]);
    expect(controller.state.layout.right).toMatchObject({view: 'design', pending: false});
  });

  it('orders a newer design-pane request after an older supplemental refresh', async () => {
    const transport = new CrossOwnedDesignTransport();
    const controller = new SocketSessionController(transport);
    await controller.start();
    await transport.settleDesign('success');

    transport.design = [designRound(1)];
    transport.emitExperimentChange(1);
    await settlePromiseEffects();
    expect(transport.designRequests).toBe(2);

    transport.design = [designRound(1), designRound(2)];
    const pane = controller.openPane('design');
    // Opening the pane retains a follow-up behind the older supplemental
    // request instead of racing it with a second publisher.
    expect(transport.designRequests).toBe(2);
    await transport.settleDesign('success');
    await settlePromiseEffects();
    expect(transport.designRequests).toBe(3);
    expect(controller.state.layout.right).toMatchObject({view: 'design', pending: true});

    await transport.settleDesign('success');
    await pane;
    await settlePromiseEffects();
    expect(controller.state.designLog?.map(round => round.round)).toEqual([1, 2]);
    expect(controller.state.layout.right).toMatchObject({view: 'design', pending: false});
  });

  it('keeps a joined design pane pending until its authoritative follow-up settles', async () => {
    const transport = new CrossOwnedDesignTransport();
    const controller = new SocketSessionController(transport);
    await controller.start();
    await transport.settleDesign('success');

    transport.design = [designRound(1)];
    transport.emitExperimentChange(1);
    await settlePromiseEffects();
    expect(transport.designRequests).toBe(2);

    transport.design = [designRound(1), designRound(2)];
    let paneSettled = false;
    const pane = controller.openPane('design').then(() => {
      paneSettled = true;
    });
    expect(transport.designRequests).toBe(2);

    await transport.settleDesign('success');
    await settlePromiseEffects();
    expect(transport.designRequests).toBe(3);
    expect(controller.state.layout.right).toMatchObject({view: 'design', pending: true});
    expect(paneSettled).toBe(false);

    await transport.settleDesign('success');
    await pane;
    expect(controller.state.designLog?.map(round => round.round)).toEqual([1, 2]);
    expect(controller.state.layout.right).toMatchObject({view: 'design', pending: false});
    expect(paneSettled).toBe(true);
  });

  it('does not surface a failed design request after another pane replaces it', async () => {
    const transport = new CrossOwnedDesignTransport();
    const controller = new SocketSessionController(transport);
    await controller.start();
    await transport.settleDesign('success');

    const design = controller.openPane('design');
    const performance = controller.openPane('perf');
    await transport.settleDesign('failure');
    await Promise.all([design, performance]);
    await settlePromiseEffects();

    expect(controller.state.layout.right).toMatchObject({view: 'perf', pending: false});
    expect(controller.state.errorBanner).toBeNull();
  });

  for (const supersededOutcome of ['success', 'failure'] as const) {
    it(`reports the final failure after a ${supersededOutcome} superseded design refresh`, async () => {
      const transport = new CrossOwnedDesignTransport();
      const controller = new SocketSessionController(transport);
      await controller.start();
      await transport.settleDesign('success');

      transport.emitExperimentChange(1);
      await settlePromiseEffects();
      const pane = controller.openPane('design');
      await transport.settleDesign(supersededOutcome);
      await settlePromiseEffects();

      // The joined request was superseded by the pane's retained follow-up, so
      // its outcome cannot discharge failure ownership while the authoritative
      // attempt is still pending.
      expect(transport.designRequests).toBe(3);
      expect(controller.state.errorBanner).toBeNull();
      await transport.settleDesign('failure');
      await pane;
      await settlePromiseEffects();

      expect(controller.state.layout.right).toMatchObject({
        view: 'design',
        pending: false,
        error: 'design query failed',
      });
      expect(controller.state.errorBanner).toMatchObject({
        scope: 'request',
        message: 'design query failed',
        count: 1,
      });
    });
  }

  it('opens the selected hypothesis with /open-round', async () => {
    const transport = new FakeTransport();
    transport.experiments = [
      entry('H-01', 1, 1, {rounds: [{round: 1, passed: true, reviewed: true}]}),
      entry('H-02', 2, 3, {
        rounds: [
          {round: 2, passed: false, reviewed: false},
          {round: 3, passed: false, reviewed: true},
        ],
      }),
    ];
    const controller = new SocketSessionController(transport);
    await controller.start();
    controller.moveExperimentSelection(1);

    await controller.submitCommand('/open-round');

    expect(controller.state.hypothesisScope).toMatchObject({id: 'H-02', rounds: [2, 3]});
    // Lands on the hypothesis's latest round: the round view is built around
    // one round, and `[` walks back through the earlier ones.
    expect(controller.state.selectedRound).toBe(3);
  });

  it('opens the hypothesis owning a round with /open-round --N', async () => {
    const transport = new FakeTransport();
    transport.experiments = [
      entry('H-01', 1, 1, {rounds: [{round: 1, passed: true, reviewed: true}]}),
      entry('H-02', 2, 3, {
        rounds: [
          {round: 2, passed: false, reviewed: false},
          {round: 3, passed: false, reviewed: true},
        ],
      }),
    ];
    const controller = new SocketSessionController(transport);
    await controller.start();

    await controller.submitCommand('/open-round --3');

    // Lands on the requested round, inside the hypothesis that owns it, and
    // moves the table selection to match.
    expect(controller.state.hypothesisScope).toMatchObject({id: 'H-02', rounds: [2, 3]});
    expect(controller.state.selectedRound).toBe(3);
    expect(controller.state.experimentLog?.selectedId).toBe('H-02');
  });

  it('opens a recorded round that belongs to no hypothesis', async () => {
    const transport = new FakeTransport();
    transport.experiments = [
      entry('H-01', 1, 1, {rounds: [{round: 1, passed: true, reviewed: true}]}),
    ];
    const controller = new SocketSessionController(transport);
    await controller.start();

    transport.emit({
      type: 'event',
      event: {...event(1, 'phase_started'), agent_kind: 'orchestrator', round_label: 'round-9-pre'},
    });

    await controller.submitCommand('/open-round --9');

    expect(controller.state.hypothesisScope).toMatchObject({id: 'round-9', rounds: [9]});
    expect(controller.state.selectedRound).toBe(9);
  });

  it('opens the first planning activity before it has a hypothesis record', async () => {
    const transport = new FakeTransport();
    const controller = new SocketSessionController(transport);
    await controller.start();
    transport.emit({
      type: 'event',
      event: {...event(1, 'phase_started'), agent_kind: 'orchestrator', round_label: 'round-1-pre'},
    });

    controller.enterExperimentDrilldown();

    expect(controller.state.hypothesisScope).toMatchObject({id: 'round-1', rounds: [1]});
    expect(controller.state.selectedRound).toBe(1);
  });

  it('reports a round that has not been observed', async () => {
    const controller = new SocketSessionController(new FakeTransport());

    await controller.submitCommand('/open-round --9');

    expect(controller.state.overlay?.content).toContain('Round 9 has not been recorded.');
    expect(controller.state.hypothesisScope).toBeNull();
  });

  it('says where it already is when /open-round runs inside a hypothesis', async () => {
    const transport = new FakeTransport();
    transport.experiments = [
      entry('H-01', 1, 1, {rounds: [{round: 1, passed: true, reviewed: true}]}),
    ];
    const controller = new SocketSessionController(transport);
    await controller.start();
    await controller.submitCommand('/open-round');

    await controller.submitCommand('/open-round');

    expect(controller.state.overlay?.content).toContain('Already inside H-01');
    expect(controller.state.hypothesisScope).toMatchObject({id: 'H-01'});
  });

  it('keeps the open pane current as benchmark measurements land', async () => {
    const transport = new FakeTransport(
      [],
      [{round: 1, perf_metric: 1200, perf_unit: 'ops', passed: true, profile_skipped: false}],
    );
    const controller = new SocketSessionController(transport);
    await controller.start();
    await controller.submitCommand('/perf');
    const before = perfRequests(transport);

    transport.emit({type: 'event', event: benchmark(9, 1300)});
    await Promise.resolve();
    await Promise.resolve();

    // The experiment log refetches on the same event; count only the pane's.
    expect(perfRequests(transport) - before).toBe(1);
  });

  it('does not refetch the pane once it is closed', async () => {
    const transport = new FakeTransport();
    const controller = new SocketSessionController(transport);
    await controller.start();
    await controller.submitCommand('/perf');
    controller.closePane();
    const before = perfRequests(transport);

    transport.emit({type: 'event', event: benchmark(9, 1300)});
    await Promise.resolve();

    expect(perfRequests(transport)).toBe(before);
    expect(controller.state.layout.right).toBeNull();
  });

  it('fetches the view opened while another view’s query is in flight', async () => {
    const transport = new DeferredDesignTransport();
    const controller = new SocketSessionController(transport);
    await controller.start();
    transport.deferDesign = true;

    const designOpen = controller.openPane('design');
    const perfOpen = controller.openPane('perf');
    // The perf pane is on screen and waiting; its query must not have been
    // swallowed by the design query still in flight.
    expect(controller.state.layout.right?.view).toBe('perf');
    expect(controller.state.layout.right?.pending).toBe(true);
    expect(transport.requests.filter(request => request.type === 'query.performance')).toHaveLength(
      0,
    );

    transport.releaseDesign();
    await Promise.all([designOpen, perfOpen]);
    await new Promise<void>(resolve => setTimeout(resolve, 0));

    expect(transport.requests.filter(request => request.type === 'query.performance')).toHaveLength(
      1,
    );
    expect(controller.state.layout.right?.view).toBe('perf');
    expect(controller.state.layout.right?.pending).toBe(false);
    expect(paneContent(controller)).toContain('Performance · total_ops_per_sec');
  });

  it('drops the superseded design answer rather than painting it over the perf pane', async () => {
    const transport = new DeferredDesignTransport();
    const controller = new SocketSessionController(transport);
    await controller.start();
    transport.deferDesign = true;

    const designOpen = controller.openPane('design');
    const perfOpen = controller.openPane('perf');
    transport.releaseDesign();
    await Promise.all([designOpen, perfOpen]);
    await new Promise<void>(resolve => setTimeout(resolve, 0));

    expect(controller.state.layout.right?.view).toBe('perf');
    expect(paneContent(controller)).toContain('Performance · total_ops_per_sec');
    expect(paneContent(controller)).not.toContain('Design changes by round');
  });

  it('coalesces same-view refreshes during a fetch into one follow-up', async () => {
    const transport = new DeferredDesignTransport();
    const controller = new SocketSessionController(transport);
    await controller.start();
    transport.deferDesign = true;

    const designOpen = controller.openPane('design');
    // Two rounds finish while the query is still running. The in-flight
    // answer predates both, so one refetch follows, not one per event.
    transport.emit(benchmark(9, 1300));
    transport.emit(benchmark(10, 1400));
    const before = designQueries(transport);

    transport.releaseDesign();
    await designOpen;
    await new Promise<void>(resolve => setTimeout(resolve, 0));
    expect(designQueries(transport) - before).toBe(1);

    transport.releaseDesign();
    await new Promise<void>(resolve => setTimeout(resolve, 0));
    expect(controller.state.layout.right?.pending).toBe(false);
    expect(designQueries(transport) - before).toBe(1);
  });

  it('closes the pane without disturbing the chat', async () => {
    const transport = new FakeTransport([], [], {
      question: 'why?',
      answer: 'Round 2 regressed.',
      effect: 'none',
    });
    const controller = new SocketSessionController(transport);
    await controller.start();
    await controller.sendChat('why?');
    await controller.submitCommand('/perf');

    controller.closePane();

    expect(controller.state.layout.right).toBeNull();
    expect(chatPaneVisible(controller.state)).toBe(true);
    expect(controller.state.chatConversation.map(entry => entry.content)).toEqual([
      'why?',
      'Round 2 regressed.',
    ]);
  });

  it('keeps the docked chat beside the log while a visualization is open', async () => {
    const transport = new FakeTransport(
      [],
      [{round: 1, perf_metric: 1200, perf_unit: 'ops', passed: true, profile_skipped: false}],
    );
    const controller = new SocketSessionController(transport);
    await controller.start();

    await controller.submitCommand('/perf');
    await controller.sendChat('what changed in r1?');

    // Three columns: chat, log, pane. None of them replaced another.
    expect(controller.state.layout.right?.view).toBe('perf');
    expect(chatPaneVisible(controller.state)).toBe(true);
    expect(experimentLogVisible(controller.state)).toBe(true);
    expect(controller.state.chatConversation.at(0)?.content).toBe('what changed in r1?');
  });

  it('sends chat messages while the pane stays put', async () => {
    const transport = new FakeTransport([], [], {
      question: 'what regressed?',
      answer: 'The sampler reorder.',
      effect: 'none',
    });
    const controller = new SocketSessionController(transport);
    await controller.start();
    await controller.submitCommand('/perf');
    const pane = controller.state.layout.right;

    await controller.sendChat('what regressed?');

    expect(controller.state.chatConversation.at(-1)?.content).toBe('The sampler reorder.');
    expect(controller.state.layout.right?.view).toBe('perf');
    expect(controller.state.layout.right).toBe(pane);
  });

  it('surfaces a failed experiment query without closing the view', async () => {
    const transport = new FakeTransport([], [], undefined, new Error('socket closed'));
    const controller = new SocketSessionController(transport);

    await controller.openExperimentLog();

    expect(controller.state.experimentLog?.error).toContain('socket closed');
    expect(controller.state.experimentLog?.pending).toBe(false);
  });

  it('runs a slash command typed in the chat through the main input path', async () => {
    const transport = new FakeTransport();
    const controller = new SocketSessionController(transport);
    await controller.submitCommand('/chat');
    const before = transport.requests.length;

    // The performance plot, which is the command that answers in the right
    // pane now that the experiment log is reached without a command.
    await controller.submitChat('/perf');

    // Handled as a command, not forwarded to the chat agent.
    expect(transport.requests.slice(before)).toEqual([{type: 'query.performance'}]);
    expect(controller.state.layout.right?.view).toBe('perf');
    expect(paneContent(controller)).toContain('No performance data yet.');
    expect(controller.state.chatConversation).toHaveLength(0);
  });

  it('shows the chat help for an unknown slash command instead of asking the agent', async () => {
    const transport = new FakeTransport();
    const controller = new SocketSessionController(transport);

    await controller.submitChat('/nope');

    // Chat-scoped help, not the global surface's "unknown command" banner.
    expect(controller.state.errorBanner).toBeNull();
    expect(controller.state.chatConversation.at(-1)?.content).toContain('/clear');
    expect(transport.requests).toEqual([]);
  });

  it('still sends ordinary questions, including text containing a slash', async () => {
    const transport = new FakeTransport([], [], {
      question: 'what changed in a/b testing?',
      answer: 'Nothing yet.',
      effect: 'none',
    });
    const controller = new SocketSessionController(transport);

    await controller.submitChat('what changed in a/b testing?');

    expect(transport.requests.at(-1)).toEqual({
      type: 'query.chat',
      text: 'what changed in a/b testing?',
    });
    expect(controller.state.chatConversation.at(-1)?.content).toBe('Nothing yet.');
  });

  it('renders /model from the backend options, grouped by harness', async () => {
    const transport = new ThreadTransport();
    const controller = new SocketSessionController(transport);
    await controller.start();

    await controller.submitChat('/model');

    expect(transport.requests.at(-1)).toEqual({type: 'query.chat_options'});
    const menu = controller.state.chatMenu;
    expect(menu?.kind).toBe('model');
    expect(menu?.pending).toBe(false);
    // Exactly what the backend returned: harness groups, their models, and one
    // free-text entry per group. The client enumerates nothing of its own.
    expect(menu?.rows.map(row => [row.kind, row.label])).toEqual([
      ['header', 'Codex'],
      ['model', 'gpt-run  \u00b7 run default'],
      ['model', 'gpt-5.6-sol'],
      ['custom', 'custom model\u2026'],
      ['header', 'Claude Code'],
      ['model', 'claude-opus-5'],
      ['custom', 'custom model\u2026'],
    ]);
    // Headers are structure, so the highlight starts on the first real choice.
    expect(menu?.selected).toBe(1);
  });

  it('starts a thread on the selected model', async () => {
    const transport = new ThreadTransport();
    const controller = new SocketSessionController(transport);
    await controller.start();

    await controller.submitChat('/model');
    controller.moveChatMenuSelection(1);
    await controller.confirmChatMenu();

    expect(transport.requests.at(-1)).toEqual({
      type: 'query.chat_thread_create',
      provider: 'codex',
      model: 'gpt-5.6-sol',
    });
    expect(controller.state.chatMenu).toBeNull();
    // The thread record comes from the replayed backend event, and the
    // client switches the chat surfaces to it.
    expect(controller.state.core.chatThreads.map(thread => thread.id)).toEqual([
      'default',
      'thread-1',
    ]);
    expect(controller.state.activeChatThreadId).toBe('thread-1');
  });

  it('accepts a typed model from a group\u2019s custom entry', async () => {
    const transport = new ThreadTransport();
    const controller = new SocketSessionController(transport);
    await controller.start();

    await controller.submitChat('/model');
    // Down to the Claude group's custom entry, skipping the group headers.
    controller.moveChatMenuSelection(4);
    expect(controller.state.chatMenu?.rows[controller.state.chatMenu.selected]).toMatchObject({
      kind: 'custom',
      provider: 'claude',
    });
    for (const character of 'claude-sonnet-5') controller.typeChatMenuCustomModel(character);
    controller.backspaceChatMenuCustomModel();
    controller.typeChatMenuCustomModel('5');
    await controller.confirmChatMenu();

    expect(transport.requests.at(-1)).toEqual({
      type: 'query.chat_thread_create',
      provider: 'claude',
      model: 'claude-sonnet-5',
    });
    expect(controller.state.activeChatThreadId).toBe('thread-1');
  });

  it('leaves an empty custom entry alone rather than guessing a model', async () => {
    const transport = new ThreadTransport();
    const controller = new SocketSessionController(transport);
    await controller.start();

    await controller.submitChat('/model');
    controller.moveChatMenuSelection(2);
    await controller.confirmChatMenu();

    expect(transport.requests.at(-1)).toEqual({type: 'query.chat_options'});
    expect(controller.state.chatMenu?.kind).toBe('model');
  });

  it('reports a chat-options failure in the menu instead of an empty list', async () => {
    const transport = new ThreadTransport();
    transport.chatOptions = null;
    const controller = new SocketSessionController(transport);
    await controller.start();

    await controller.submitChat('/model');

    expect(controller.state.chatMenu?.error).toContain('has not reported its chat options');
    expect(controller.state.chatMenu?.selected).toBe(-1);
  });

  it('ignores a chat-options failure after the operator opens the thread menu', async () => {
    const transport = new DeferredChatOptionsTransport();
    const controller = new SocketSessionController(transport);
    await controller.start();

    const loading = controller.openChatModelMenu();
    controller.openChatResumeMenu();
    const threadMenu = controller.state.chatMenu;
    transport.rejectNext(new Error('options unavailable'));
    await loading;

    expect(controller.state.chatMenu).toBe(threadMenu);
    expect(controller.state.chatMenu?.kind).toBe('resume');
    expect(controller.state.errorBanner).toBeNull();
  });

  it('lets only the newest chat-options request fill the model menu', async () => {
    const transport = new DeferredChatOptionsTransport();
    const controller = new SocketSessionController(transport);
    await controller.start();

    const first = controller.openChatModelMenu();
    const second = controller.openChatModelMenu();
    transport.resolveNext('stale-model');
    await first;

    expect(controller.state.chatMenu?.rows).toEqual([{kind: 'note', label: 'Loading options…'}]);

    transport.resolveNext('current-model');
    await second;
    expect(controller.state.chatMenu?.rows.map(row => row.label)).toContain('current-model');
    expect(controller.state.chatMenu?.rows.map(row => row.label)).not.toContain('stale-model');
  });

  it('/clear starts a fresh thread on the current thread\u2019s settings', async () => {
    const transport = new ThreadTransport();
    const controller = new SocketSessionController(transport);
    await controller.start();
    await controller.submitChat('/model');
    controller.moveChatMenuSelection(1);
    await controller.confirmChatMenu();
    await controller.sendChat('what changed?');

    await controller.submitChat('/clear');

    // Same harness and model, a new thread, and the old one still listed.
    expect(transport.requests.at(-1)).toEqual({
      type: 'query.chat_thread_create',
      provider: 'codex',
      model: 'gpt-5.6-sol',
    });
    expect(controller.state.activeChatThreadId).toBe('thread-2');
    expect(controller.state.core.chatThreads.map(thread => thread.id)).toEqual([
      'default',
      'thread-1',
      'thread-2',
    ]);
    // The cleared thread keeps its transcript, so /resume gets it back intact.
    expect(controller.state.chatConversations['thread-1']?.map(item => item.content)).toEqual([
      'what changed?',
      'Thread answer.',
    ]);
    expect(controller.state.chatConversation).toEqual([]);
  });

  it('/clear on the default thread lets the backend resolve the run settings', async () => {
    const transport = new ThreadTransport();
    const controller = new SocketSessionController(transport);
    await controller.start();

    await controller.submitChat('/clear');

    expect(transport.requests.at(-1)).toEqual({type: 'query.chat_thread_create'});
  });

  it('keeps the newest thread-create selection when an older reply arrives last', async () => {
    const transport = new DeferredThreadCreateTransport();
    const controller = new SocketSessionController(transport);
    await controller.start();

    const older = controller.clearChatThread();
    const newer = controller.clearChatThread();
    transport.resolveCreate(1, 'thread-newer', 2);
    await newer;
    expect(controller.state.activeChatThreadId).toBe('thread-newer');

    transport.resolveCreate(0, 'thread-older', 1);
    await older;

    expect(controller.state.activeChatThreadId).toBe('thread-newer');
    expect(controller.state.core.chatThreads.map(thread => thread.id)).toEqual([
      'default',
      'thread-older',
      'thread-newer',
    ]);
  });

  it('does not publish a pending thread-create failure while stopping', async () => {
    const transport = new DeferredThreadCreateTransport();
    transport.rejectCreatesOnClose = new Error('Client closed');
    const controller = new SocketSessionController(transport);
    await controller.start();

    const creating = controller.clearChatThread();
    await controller.stop();
    await creating;

    expect(controller.state.errorBanner).toBeNull();
    expect(controller.state.activeChatThreadId).toBe('default');
  });

  it('does not give a thread-create reply navigation ownership after stopping', async () => {
    const transport = new DeferredThreadCreateTransport();
    const controller = new SocketSessionController(transport);
    await controller.start();

    const creating = controller.clearChatThread();
    await controller.stop();
    transport.resolveCreate(0, 'thread-created', 1);
    await creating;

    expect(controller.state.activeChatThreadId).toBe('default');
    expect(controller.state.core.chatThreads.map(thread => thread.id)).toContain('thread-created');
  });

  it('does not let a pending thread-create reply override manual thread navigation', async () => {
    const transport = new DeferredThreadCreateTransport();
    const controller = new SocketSessionController(transport);
    await controller.start();

    const creating = controller.clearChatThread();
    controller.switchChatThread('default');
    transport.resolveCreate(0, 'thread-created', 1);
    await creating;

    expect(controller.state.activeChatThreadId).toBe('default');
    expect(controller.state.core.chatThreads.map(thread => thread.id)).toContain('thread-created');
  });

  it('keeps a newer thread menu open when a pending thread-create reply settles', async () => {
    const transport = new DeferredThreadCreateTransport();
    const controller = new SocketSessionController(transport);
    await controller.start();

    const creating = controller.clearChatThread();
    controller.openChatResumeMenu();
    transport.resolveCreate(0, 'thread-created', 1);
    await creating;

    expect(controller.state.activeChatThreadId).toBe('default');
    expect(controller.state.chatMenu?.kind).toBe('resume');
    expect(controller.state.chatMenu?.rows.map(row => row.kind)).toContain('thread');
  });

  it('does not report an older thread-create failure over a newer success', async () => {
    const transport = new DeferredThreadCreateTransport();
    const controller = new SocketSessionController(transport);
    await controller.start();

    const older = controller.clearChatThread();
    const newer = controller.clearChatThread();
    transport.resolveCreate(1, 'thread-newer', 1);
    await newer;
    transport.rejectCreate(0, new Error('older create failed'));
    await older;

    expect(controller.state.activeChatThreadId).toBe('thread-newer');
    expect(controller.state.errorBanner).toBeNull();
  });

  it('answers unknown slash input in the composer with the chat help', async () => {
    const transport = new ThreadTransport();
    const controller = new SocketSessionController(transport);
    await controller.start();
    const before = transport.requests.length;

    await controller.submitChat('/threads');

    // No request at all, and no global "unknown command" error banner.
    expect(transport.requests.length).toBe(before);
    expect(controller.state.errorBanner).toBeNull();
    expect(controller.state.chatConversation.at(-1)?.content).toContain('/model');
  });

  it('still forwards a global command typed into the composer', async () => {
    const transport = new ThreadTransport();
    const controller = new SocketSessionController(transport);
    await controller.start();

    await controller.submitChat('/pause');

    expect(transport.requests.at(-1)).toEqual({type: 'command.pause'});
  });

  it('sends chat to the active thread and keeps transcripts apart', async () => {
    const transport = new ThreadTransport();
    const controller = new SocketSessionController(transport);
    await controller.start();
    await controller.submitChat('/model');
    await controller.confirmChatMenu();
    expect(controller.state.activeChatThreadId).toBe('thread-1');

    await controller.sendChat('which kernel changed?');

    expect(transport.requests.at(-1)).toEqual({
      type: 'query.chat',
      text: 'which kernel changed?',
      thread_id: 'thread-1',
    });
    expect(controller.state.chatConversations['thread-1']?.map(entry => entry.content)).toEqual([
      'which kernel changed?',
      'Thread answer.',
    ]);
    expect(controller.state.chatConversations['default'] ?? []).toEqual([]);

    // Switching swaps what the singular selectors show; nothing is lost.
    controller.switchChatThread('default');
    expect(controller.state.chatConversation).toEqual([]);
    await controller.sendChat('and the default thread?');
    expect(transport.requests.at(-1)).toEqual({
      type: 'query.chat',
      text: 'and the default thread?',
    });
    controller.switchChatThread('thread-1');
    expect(controller.state.chatConversation.map(entry => entry.content)).toEqual([
      'which kernel changed?',
      'Thread answer.',
    ]);
  });

  it('/switch lists the threads with their runtime and switches', async () => {
    const transport = new ThreadTransport();
    const controller = new SocketSessionController(transport);
    await controller.start();
    await controller.submitChat('/model');
    await controller.confirmChatMenu();
    controller.switchChatThread('default');

    await controller.submitChat('/switch');

    const menu = controller.state.chatMenu;
    expect(menu?.kind).toBe('resume');
    expect(menu?.rows.map(row => [row.kind, row.label])).toEqual([
      ['thread', 'Experiment chat'],
      ['thread', 'Codex (GPT Run)'],
    ]);
    // The runtime is spelled out beside each thread, harness and model only.
    expect(menu?.rows.map(row => (row.kind === 'thread' ? row.detail : null))).toEqual([
      'run agent',
      'Codex (GPT Run)',
    ]);
    // The highlight starts on the thread that is currently on screen.
    expect(menu?.selected).toBe(0);

    controller.moveChatMenuSelection(1);
    await controller.confirmChatMenu();

    expect(controller.state.chatMenu).toBeNull();
    expect(controller.state.activeChatThreadId).toBe('thread-1');
  });

  it('resumes the paused run from the chat, same as the command bar', async () => {
    const transport = new ThreadTransport();
    const controller = new SocketSessionController(transport);
    await controller.start();

    await controller.submitChat('/resume');

    // The chat forwards /resume to the one command executor: it resumes the run
    // and opens no thread menu.
    expect(transport.requests).toContainEqual({type: 'command.resume'});
    expect(controller.state.chatMenu).toBeNull();
  });

  it("reports a chat-only command's usage error rather than an unknown-command error", async () => {
    const transport = new ThreadTransport();
    const controller = new SocketSessionController(transport);
    await controller.start();

    // /clear takes no argument. The chat resolves it, so the error must be the
    // registry usage error; re-parsing the text on the command bar, which does
    // not offer /clear, would report "Unknown command" instead.
    const before = transport.requests.length;
    await controller.submitChat('/clear definitely-not');

    // A usage error is `scope: 'input'` regardless of which surface typed it,
    // so it lands on the command input's hint rather than the banner.
    expect(controller.state.inputError).toBe('Usage: /clear');
    expect(controller.state.errorBanner).toBeNull();
    // The phrase started no thread and sent no request.
    expect(transport.requests.length).toBe(before);
    expect(controller.state.chatMenu).toBeNull();
  });

  it('does not run a no-argument command typed with a trailing phrase', async () => {
    const transport = new FakeTransport();
    const controller = new SocketSessionController(transport);

    await controller.submitCommand('/pause typo');

    expect(controller.state.inputError).toBe('Usage: /pause');
    expect(controller.state.errorBanner).toBeNull();
    expect(transport.requests).toEqual([]);
  });

  it('reports an unknown theme as an error without switching', async () => {
    const transport = new FakeTransport();
    const controller = new SocketSessionController(transport);

    await controller.submitCommand('/theme monokai');

    expect(controller.state.errorBanner).toBeNull();
    expect(controller.state.inputError).toContain('Unknown theme: monokai');
    expect(controller.state.themeName).toBe('dark');
    expect(transport.requests).toEqual([]);
  });

  it('boots against the tail of the stream rather than the whole history', async () => {
    const transport = new HistoryTransport();
    const controller = new SocketSessionController(transport);

    await controller.start();

    expect(transport.subscribeTails).toEqual([1_000]);
  });

  it('falls back once to a full subscription when the tail is rejected', async () => {
    const history = [
      event(1, 'agent_output_chunk', 'one\n'),
      event(2, 'agent_output_chunk', 'two\n'),
    ];
    const transport = new HistoryTransport(history);
    transport.rejectTail = true;
    const controller = new SocketSessionController(transport);

    await controller.start();

    // The rejection is the capability probe, so there is exactly one retry and
    // it carries no tail.
    expect(transport.subscribeTails).toEqual([1_000, undefined]);

    transport.emitBatch(history, 0);

    expect(controller.state.core.transcript.map(item => item.content).join('')).toBe('one\ntwo\n');
    expect(controller.state.core.historyAfterSequence).toBe(0);
    // A boot that recovered is not a boot that failed.
    expect(controller.state.errorBanner).toBeNull();
    expect(controller.state.eventStreamAvailable).toBe(true);
  });

  it('reports the transport error when the full subscription fails too', async () => {
    const transport = new HistoryTransport();
    transport.rejectTail = true;
    transport.subscribeError = new Error('Server is disconnected');
    const controller = new SocketSessionController(transport);

    await controller.start();

    expect(transport.subscribeTails).toEqual([1_000, undefined]);
    expect(controller.state.eventStreamAvailable).toBe(false);
    expect(controller.state.errorBanner).toMatchObject({scope: 'transport'});
  });

  it('backfills a legacy batch without sending the unsupported store-fence key', async () => {
    const history = longHistory(2_000);
    const transport = new HistoryTransport(history);
    const controller = new SocketSessionController(transport);
    await controller.start();

    transport.emitBatch(history.slice(1_500), 1_500);

    expect(controller.state.core.historyAfterSequence).toBe(1_500);
    expect(controller.state.core.transcript).toHaveLength(500);

    await expect(controller.loadOlderHistory()).resolves.toBe(true);

    expect(eventsQueries(transport)).toEqual([
      {
        type: 'query.events',
        after_sequence: 500,
        before_sequence: 1_501,
      },
    ]);
    expect(controller.state.core.historyAfterSequence).toBe(500);
    expect(controller.state.core.transcript).toHaveLength(1_500);
  });

  it('retries a backfill range whose prefix belongs to another run', async () => {
    const foreign = {...event(1_000, 'agent_output_chunk', 'foreign\n'), run_id: 'run-b'};
    const transport = new HistoryTransport([foreign]);
    const controller = new SocketSessionController(transport);
    await controller.start();
    transport.emitBatch(
      [{...event(1_501, 'agent_output_chunk', 'tail\n'), run_id: 'run-a'}],
      1_500,
    );

    await expect(controller.loadOlderHistory()).resolves.toBe(false);

    expect(controller.state.core.historyAfterSequence).toBe(1_500);
    expect(
      controller.state.core.diagnostics.some(item => item.code === 'run_identity_mismatch'),
    ).toBe(true);

    await expect(controller.loadOlderHistory()).resolves.toBe(false);
    expect(eventsQueries(transport)).toEqual([
      {
        type: 'query.events',
        after_sequence: 500,
        before_sequence: 1_501,
      },
      {
        type: 'query.events',
        after_sequence: 500,
        before_sequence: 1_501,
      },
    ]);
    expect(controller.state.core.historyAfterSequence).toBe(1_500);
  });

  it('stops asking once the history floor reaches the start of the run', async () => {
    const history = longHistory(2_000);
    const transport = new HistoryTransport(history);
    const controller = new SocketSessionController(transport);
    await controller.start();
    transport.emitBatch(history.slice(1_500), 1_500);

    await controller.loadOlderHistory();
    await expect(controller.loadOlderHistory()).resolves.toBe(true);

    expect(controller.state.core.historyAfterSequence).toBe(0);
    expect(controller.state.core.transcript).toHaveLength(2_000);
    const issued = eventsQueries(transport).length;

    await expect(controller.loadOlderHistory()).resolves.toBe(false);

    expect(eventsQueries(transport)).toHaveLength(issued);

    // Every batch of the subscription repeats the floor it bootstrapped with,
    // which must not undo the backfill and send the client back for history it
    // already holds.
    transport.emitBatch([event(2_001, 'agent_output_chunk', 'live\n')], 1_500);

    expect(controller.state.core.historyAfterSequence).toBe(0);
    await expect(controller.loadOlderHistory()).resolves.toBe(false);
    expect(eventsQueries(transport)).toHaveLength(issued);
  });

  it('drops a store-fenced backfill rejected before the replacement batch arrives', async () => {
    const history = longHistory(2_000);
    const transport = new HistoryTransport(history);
    const controller = new SocketSessionController(transport);
    await controller.start();
    transport.emitBatch(history.slice(1_500), 1_500, 'store-a');
    transport.eventsError = new ServerError(
      'The run event store changed before history was captured',
      {
        code: 'events_store_replaced',
        summary: 'Request failed',
        scope: 'request',
      },
    );

    // Store B is already attached at the server, but its replacement batch has
    // not reached this controller. The A fence prevents B's identically
    // numbered events from being prepended into the A fold.
    await expect(controller.loadOlderHistory()).resolves.toBe(false);

    expect(eventsQueries(transport)).toEqual([
      {
        type: 'query.events',
        expected_store_id: 'store-a',
        after_sequence: 500,
        before_sequence: 1_501,
      },
    ]);
    expect(controller.state.core.historyAfterSequence).toBe(1_500);
    expect(controller.state.core.transcript).toHaveLength(500);
    expect(controller.state.errorBanner).toBeNull();
  });

  it('leaves the history floor where it was when a backfill fails', async () => {
    const history = longHistory(2_000);
    const transport = new HistoryTransport(history);
    const controller = new SocketSessionController(transport);
    await controller.start();
    transport.emitBatch(history.slice(1_500), 1_500);
    transport.eventsError = new Error('The event store is unavailable');

    await expect(controller.loadOlderHistory()).resolves.toBe(false);

    expect(controller.state.core.historyAfterSequence).toBe(1_500);
    expect(controller.state.errorBanner).toMatchObject({scope: 'request'});
    expect(controller.state.errorBanner?.message).toContain('The event store is unavailable');

    // The unchanged floor is what makes the same range retryable.
    transport.eventsError = null;
    await expect(controller.loadOlderHistory()).resolves.toBe(true);
    expect(controller.state.core.historyAfterSequence).toBe(500);
  });

  it('backfills once under concurrent requests', async () => {
    const history = longHistory(2_000);
    const transport = new HistoryTransport(history);
    const controller = new SocketSessionController(transport);
    await controller.start();
    transport.emitBatch(history.slice(1_500), 1_500);
    transport.deferEvents = true;

    const first = controller.loadOlderHistory();
    const second = controller.loadOlderHistory();

    expect(eventsQueries(transport)).toHaveLength(1);

    transport.releaseEvents();

    await expect(first).resolves.toBe(true);
    await expect(second).resolves.toBe(true);
    expect(controller.state.core.historyAfterSequence).toBe(500);
  });

  it('folds a spine event re-delivered by a backfill exactly once', async () => {
    // A tail subscription replays the run-level spine from below the floor, so
    // the backfill covering that range delivers those events a second time.
    const firstRound = roundFinished(2, 1);
    const secondRound = roundFinished(4, 2);
    const tail = event(5, 'agent_output_chunk', 'five\n');
    const history = [
      event(1, 'agent_output_chunk', 'one\n'),
      firstRound,
      event(3, 'agent_output_chunk', 'three\n'),
      secondRound,
      tail,
    ];
    const replayedTransport = new HistoryTransport(history);
    const replayed = new SocketSessionController(replayedTransport);
    await replayed.start();
    replayedTransport.emitBatch(history, 0);

    const tailedTransport = new HistoryTransport(history);
    const tailed = new SocketSessionController(tailedTransport);
    await tailed.start();
    // The spine (both `round_finished` events) below the floor, then the tail.
    tailedTransport.emitBatch([firstRound, secondRound, tail], 4);
    await tailed.loadOlderHistory();

    expect(tailed.state.core.historyAfterSequence).toBe(0);
    expect(tailed.state.core.transcript).toEqual(replayed.state.core.transcript);
    expect(tailed.state.core.rounds).toEqual(replayed.state.core.rounds);
  });
});

/**
 * The private notepad (#805): a local, per-run scratchpad that never reaches
 * an agent except through the two explicit "promote to draft" actions, and
 * even then only once the operator sends the pre-filled composer themselves.
 * `FakeTransport`'s snapshot always answers `run_id: 'run'` (below), so every
 * test here starts with `controller.start()` to latch that id before opening
 * the notepad.
 */
describe('notepad', () => {
  let stateHome: string;
  const savedStateHome = process.env['VIBESYS_STATE_HOME'];

  beforeEach(() => {
    stateHome = mkdtempSync(join(tmpdir(), 'vs-controller-notepad-test-'));
    process.env['VIBESYS_STATE_HOME'] = stateHome;
  });

  afterEach(() => {
    if (savedStateHome === undefined) delete process.env['VIBESYS_STATE_HOME'];
    else process.env['VIBESYS_STATE_HOME'] = savedStateHome;
    rmSync(stateHome, {recursive: true, force: true});
  });

  it('opens empty for a run with no saved note', async () => {
    const controller = new SocketSessionController(new FakeTransport());
    await controller.start();

    controller.openNotepad();

    expect(controller.state.notepad.open).toBe(true);
    expect(controller.state.notepad.text).toBe('');
  });

  it('hydrates from a note already saved for this run', async () => {
    writeNote({
      projectScope: NOTE_PROJECT_SCOPE,
      runId: 'run',
      text: 'from a previous session',
      createdAt: 't0',
      updatedAt: 't0',
    });
    const controller = new SocketSessionController(new FakeTransport());
    await controller.start();

    controller.openNotepad();

    expect(controller.state.notepad.text).toBe('from a previous session');
  });

  it('hydrates an untouched notepad opened before startup learns its identity', async () => {
    writeNote({
      projectScope: NOTE_PROJECT_SCOPE,
      runId: 'run',
      text: 'saved before restart',
      createdAt: 't0',
      updatedAt: 't0',
    });
    const transport = new FakeTransport();
    transport.deferSnapshot = true;
    const controller = new SocketSessionController(transport);

    const starting = controller.start();
    controller.openNotepad();
    expect(controller.state.notepad).toMatchObject({
      open: true,
      text: '',
      persistence: 'pending_identity',
    });

    transport.releaseSnapshot();
    await starting;

    expect(controller.state.notepad).toMatchObject({
      open: true,
      text: 'saved before restart',
      createdAt: 't0',
      updatedAt: 't0',
      persistence: 'ready',
    });
  });

  it('preserves a touched startup draft without overwriting an existing saved note', async () => {
    writeNote({
      projectScope: NOTE_PROJECT_SCOPE,
      runId: 'run',
      text: 'saved before restart',
      createdAt: 'saved-t0',
      updatedAt: 'saved-t1',
    });
    const transport = new FakeTransport();
    transport.deferSnapshot = true;
    const controller = new SocketSessionController(transport);

    const starting = controller.start();
    controller.openNotepad();
    controller.setNoteText('draft written during startup');
    expect(readNote({projectScope: NOTE_PROJECT_SCOPE, runId: 'run'})?.text).toBe(
      'saved before restart',
    );

    transport.releaseSnapshot();
    await starting;

    expect(controller.state.notepad).toMatchObject({
      open: true,
      text: 'draft written during startup',
      persistence: 'conflict',
    });
    controller.setNoteText('edited conflict draft');
    expect(readNote({projectScope: NOTE_PROJECT_SCOPE, runId: 'run'})?.text).toBe(
      'saved before restart',
    );

    controller.closeNotepad();
    controller.openNotepad();
    expect(controller.state.notepad).toMatchObject({
      text: 'edited conflict draft',
      persistence: 'conflict',
    });

    expect(controller.promoteNoteToSteerDraft()).toEqual({text: 'edited conflict draft'});
    controller.openNotepad();
    expect(controller.state.notepad).toMatchObject({
      text: 'saved before restart',
      persistence: 'ready',
    });
  });

  it('lets either promotion action retire a conflicted draft that was edited to blank', async () => {
    for (const promotion of ['steer', 'chat'] as const) {
      writeNote({
        projectScope: NOTE_PROJECT_SCOPE,
        runId: 'run',
        text: 'saved before restart',
        createdAt: 'saved-t0',
        updatedAt: 'saved-t1',
      });
      const transport = new FakeTransport();
      transport.deferSnapshot = true;
      const controller = new SocketSessionController(transport);

      const starting = controller.start();
      controller.openNotepad();
      controller.setNoteText('startup draft');
      transport.releaseSnapshot();
      await starting;
      controller.setNoteText('   ');
      expect(controller.state.notepad.persistence).toBe('conflict');

      const prefill =
        promotion === 'steer'
          ? controller.promoteNoteToSteerDraft()
          : controller.promoteNoteToChatDraft();

      expect(prefill).toBeNull();
      expect(controller.state.notepad.open).toBe(false);
      expect(readNote({projectScope: NOTE_PROJECT_SCOPE, runId: 'run'})?.text).toBe(
        'saved before restart',
      );
      controller.openNotepad();
      expect(controller.state.notepad).toMatchObject({
        text: 'saved before restart',
        persistence: 'ready',
      });
    }
  });

  it('persists a touched startup draft when its resolved identity has no saved note', async () => {
    const transport = new FakeTransport();
    transport.deferSnapshot = true;
    const controller = new SocketSessionController(transport);

    const starting = controller.start();
    controller.openNotepad();
    controller.setNoteText('first note for this run');
    transport.releaseSnapshot();
    await starting;

    expect(controller.state.notepad.persistence).toBe('ready');
    expect(readNote({projectScope: NOTE_PROJECT_SCOPE, runId: 'run'})?.text).toBe(
      'first note for this run',
    );
  });

  it('keeps a startup draft when its first identity-bound write fails and retries later', async () => {
    const blockedStateHome = join(stateHome, 'startup-state-home');
    writeFileSync(blockedStateHome, 'occupied');
    process.env['VIBESYS_STATE_HOME'] = blockedStateHome;
    const transport = new FakeTransport();
    transport.deferSnapshot = true;
    const controller = new SocketSessionController(transport);

    const starting = controller.start();
    controller.openNotepad();
    controller.setNoteText('draft written during startup');
    transport.releaseSnapshot();

    await expect(starting).resolves.toBeUndefined();
    expect(controller.state.notepad).toMatchObject({
      text: 'draft written during startup',
      persistence: 'ready',
    });
    expect(controller.state.notepad.persistenceError).toContain('ENOTDIR');

    rmSync(blockedStateHome);
    mkdirSync(blockedStateHome);
    controller.setNoteText('recovered startup draft');

    expect(controller.state.notepad.persistenceError).toBeNull();
    expect(readNote({projectScope: NOTE_PROJECT_SCOPE, runId: 'run'})?.text).toBe(
      'recovered startup draft',
    );
  });

  it('persists every keystroke, surviving a restart against the same run', async () => {
    const controller = new SocketSessionController(new FakeTransport());
    await controller.start();
    controller.openNotepad();

    controller.setNoteText('watch the retry budget');

    // A read with nothing cached, as a fresh process attached to the same
    // run would do.
    expect(readNote({projectScope: NOTE_PROJECT_SCOPE, runId: 'run'})?.text).toBe(
      'watch the retry budget',
    );

    const restarted = new SocketSessionController(new FakeTransport());
    await restarted.start();
    restarted.openNotepad();
    expect(restarted.state.notepad.text).toBe('watch the retry budget');
  });

  it('keeps a draft in memory and off the wire when local persistence fails', async () => {
    const blockedStateHome = join(stateHome, 'not-a-directory');
    writeFileSync(blockedStateHome, 'occupied');
    process.env['VIBESYS_STATE_HOME'] = blockedStateHome;
    const transport = new FakeTransport();
    const controller = new SocketSessionController(transport);
    await controller.start();
    const bootRequests = transport.requests.length;
    controller.openNotepad();

    expect(() => controller.setNoteText('draft stays in memory')).not.toThrow();

    expect(controller.state.notepad.text).toBe('draft stays in memory');
    expect(controller.state.notepad.persistenceError).toContain('ENOTDIR');
    expect(transport.requests.slice(bootRequests)).toEqual([]);
  });

  it('clears the persistence diagnostic after a later edit writes successfully', async () => {
    const blockedStateHome = join(stateHome, 'recoverable-state-home');
    writeFileSync(blockedStateHome, 'occupied');
    process.env['VIBESYS_STATE_HOME'] = blockedStateHome;
    const controller = new SocketSessionController(new FakeTransport());
    await controller.start();
    controller.openNotepad();
    controller.setNoteText('first draft');
    expect(controller.state.notepad.persistenceError).not.toBeNull();

    rmSync(blockedStateHome);
    mkdirSync(blockedStateHome);
    controller.setNoteText('recovered draft');

    expect(controller.state.notepad.persistenceError).toBeNull();
    expect(readNote({projectScope: NOTE_PROJECT_SCOPE, runId: 'run'})?.text).toBe(
      'recovered draft',
    );
  });

  it('does not write to disk before a run id is known', () => {
    const controller = new SocketSessionController(new FakeTransport());
    // No start(): no snapshot has landed, so there is no run id yet.
    controller.openNotepad();

    controller.setNoteText('too early to save');

    expect(controller.state.notepad.text).toBe('too early to save');
    expect(readNote({projectScope: NOTE_PROJECT_SCOPE, runId: 'run'})).toBeNull();
  });

  it('keeps controllers for the same run id in different projects isolated', async () => {
    const projectA = new FakeTransport();
    const projectB = new FakeTransport();
    projectB.projectScope = 'b'.repeat(64);
    const controllerA = new SocketSessionController(projectA);
    const controllerB = new SocketSessionController(projectB);
    await Promise.all([controllerA.start(), controllerB.start()]);
    controllerA.openNotepad();
    controllerA.setNoteText('private to project A');

    controllerB.openNotepad();
    expect(controllerB.state.notepad.text).toBe('');
    controllerB.setNoteText('private to project B');

    const restartedA = new SocketSessionController(projectA);
    await restartedA.start();
    restartedA.openNotepad();
    expect(restartedA.state.notepad.text).toBe('private to project A');
    expect(controllerB.state.notepad.text).toBe('private to project B');
  });

  it('fails closed when an older snapshot has no project scope', async () => {
    const transport = new FakeTransport();
    transport.projectScope = undefined;
    const controller = new SocketSessionController(transport);
    await controller.start();
    controller.openNotepad();

    controller.setNoteText('must not enter a global run-only file');

    expect(controller.state.notepad.text).toBe('must not enter a global run-only file');
    expect(readNote({projectScope: NOTE_PROJECT_SCOPE, runId: 'run'})).toBeNull();
  });

  it('promotes to a steer draft: closes the notepad, returns the trimmed text, sends nothing', async () => {
    const transport = new FakeTransport();
    const controller = new SocketSessionController(transport);
    await controller.start();
    const bootRequests = transport.requests.length;
    controller.openNotepad();
    controller.setNoteText('  fix the cache invalidation  ');

    const prefill = controller.promoteNoteToSteerDraft();

    expect(prefill).toEqual({text: 'fix the cache invalidation'});
    expect(controller.state.notepad.open).toBe(false);
    expect(transport.requests.slice(bootRequests)).toEqual([]);
  });

  it('promotes to a chat draft: closes the notepad, focuses chat, sends nothing', async () => {
    const transport = new FakeTransport();
    const controller = new SocketSessionController(transport);
    await controller.start();
    const bootRequests = transport.requests.length;
    controller.openNotepad();
    controller.setNoteText('ask about the flaky retry');

    const prefill = controller.promoteNoteToChatDraft();

    expect(prefill).toEqual({text: 'ask about the flaky retry'});
    expect(controller.state.notepad.open).toBe(false);
    // Chat is docked by default (the landing view), so opening it focuses the
    // dock rather than setting the standalone `chatOpen` modal flag; either
    // way, this is the same `openChat` a chat-surface command runs.
    expect(chatPaneFocused(controller.state)).toBe(true);
    expect(transport.requests.slice(bootRequests)).toEqual([]);
  });

  it('returns null for an empty or whitespace-only note, leaving the notepad open', async () => {
    const controller = new SocketSessionController(new FakeTransport());
    await controller.start();
    controller.openNotepad();

    expect(controller.promoteNoteToSteerDraft()).toBeNull();
    expect(controller.promoteNoteToChatDraft()).toBeNull();
    expect(controller.state.notepad.open).toBe(true);
  });

  /**
   * The hard requirement: a note reaches an agent only through an explicit
   * promotion the operator then explicitly sends, never as a side effect of
   * writing it, opening it, or promoting it.
   */
  it('never lands notepad text in a backend request unless the operator explicitly sends it', async () => {
    const transport = new FakeTransport();
    const controller = new SocketSessionController(transport);
    await controller.start();
    const bootRequests = transport.requests.length;
    controller.openNotepad();
    controller.setNoteText('SECRET_ONLY_FOR_MY_EYES');

    const prefill = controller.promoteNoteToSteerDraft();
    expect(prefill?.text).toBe('SECRET_ONLY_FOR_MY_EYES');
    // Writing, opening, and promoting the note are all purely local.
    expect(transport.requests.slice(bootRequests)).toEqual([]);

    // Unrelated traffic must not carry it either.
    await controller.submitCommand('/steer an unrelated instruction');
    await controller.sendChat('an unrelated question');
    expect(JSON.stringify(transport.requests)).not.toContain('SECRET_ONLY_FOR_MY_EYES');

    // Only the operator explicitly sending the pre-filled composer (here,
    // `/steer` with the promoted text, exactly as `app.ts` fills the command
    // bar) puts it on the wire.
    await controller.submitCommand(`/steer ${prefill?.text}`);
    expect(JSON.stringify(transport.requests)).toContain('SECRET_ONLY_FOR_MY_EYES');
  });
});

/**
 * The run's durable event log is attached after the client subscribes, so the
 * subscription's first batch comes from the server's own short log and the
 * stream then re-bootstraps at a tail of the run log. The two batches number
 * different logs, and the second declares a floor above the first.
 */
describe('a stream that re-bootstraps at a raised floor', () => {
  /** The run log, whose last event is the pre-attach one carried into it. */
  const runLog: RunEvent[] = [
    {
      ...event(1, 'run_started'),
      data: {kind: 'run_started', outer_loop: 'agent', input: '.', max_rounds: 3},
    },
    event(2, 'agent_output_chunk', 'two\n'),
    roundFinished(3, 1),
    event(4, 'agent_output_chunk', 'four\n'),
    event(5, 'agent_output_chunk', 'five\n'),
    event(6, 'agent_output_chunk', 'server started\n'),
  ];
  /** What the stream sends once the run log is attached: spine, then tail. */
  const rebootstrap = [runLog[0], runLog[2], runLog[4], runLog[5]] as RunEvent[];
  const preAttach = [event(1, 'agent_output_chunk', 'server started\n')];

  async function rebootstrapped(transport: HistoryTransport): Promise<SocketSessionController> {
    const controller = new SocketSessionController(transport);
    await controller.start();
    transport.emitBatch(preAttach, 0);
    transport.emitBatch(rebootstrap, 4);
    return controller;
  }

  it('keeps the raised floor so the skipped history is still backfillable', async () => {
    const transport = new HistoryTransport(runLog);

    const controller = await rebootstrapped(transport);

    expect(controller.state.core.historyAfterSequence).toBe(4);
    await expect(controller.loadOlderHistory()).resolves.toBe(true);
    expect(eventsQueries(transport)).toEqual([
      {type: 'query.events', after_sequence: 0, before_sequence: 5},
    ]);
  });

  it('folds the spine the pre-attach cursor would otherwise have swallowed', async () => {
    const controller = await rebootstrapped(new HistoryTransport(runLog));

    // `run_started` and `round_finished` sit at or below the cursor the
    // superseded log left behind, in a sequence space that no longer applies.
    expect(controller.state.core.maxRounds).toBe(3);
    expect(controller.state.core.outerLoop).toBe('agent');
    expect(controller.state.core.rounds.map(round => round.number)).toEqual([1]);
  });

  it('reaches the state a full replay of the run log would have built', async () => {
    const replayedTransport = new HistoryTransport(runLog);
    const replayed = new SocketSessionController(replayedTransport);
    await replayed.start();
    replayedTransport.emitBatch(runLog, 0);

    const controller = await rebootstrapped(new HistoryTransport(runLog));
    await controller.loadOlderHistory();

    expect(controller.state.core.historyAfterSequence).toBe(0);
    expect(controller.state.core.transcript).toEqual(replayed.state.core.transcript);
    expect(controller.state.core.rounds).toEqual(replayed.state.core.rounds);
    expect(controller.state.core.sequence).toBe(replayed.state.core.sequence);
  });

  it('refreshes experiments from a change buried in the re-bootstrap batch', async () => {
    const transport = new FakeTransport();
    const controller = new SocketSessionController(transport);
    await controller.start();
    transport.emit({type: 'event_batch', events: preAttach, history_after_sequence: 0});
    const before = transport.requests.length;

    transport.emit({
      type: 'event_batch',
      events: [...rebootstrap, event(7, 'experiments_changed')],
      history_after_sequence: 4,
    });
    await Promise.resolve();
    await Promise.resolve();

    expect(controller.state.core.historyAfterSequence).toBe(4);
    expect(
      transport.requests.slice(before).filter(request => request.type === 'query.experiments'),
    ).toHaveLength(1);
  });
});

/**
 * The same late attach, but the run log is shorter than the subscription's
 * tail, so the re-bootstrap replays it whole and declares floor 0 like the
 * batch before it. Nothing about the floors distinguishes the two logs; only
 * the store they name does.
 */
describe('a stream that re-bootstraps into a log shorter than the tail', () => {
  const runLog: RunEvent[] = [
    {
      ...event(1, 'run_started'),
      data: {kind: 'run_started', outer_loop: 'agent', input: '.', max_rounds: 3},
    },
    event(2, 'agent_output_chunk', 'two\n'),
    roundFinished(3, 1),
    event(4, 'agent_output_chunk', 'four\n'),
  ];
  // What the client folded from the server's own log before the attach: the
  // same sequence numbers, different events.
  const preAttach = [
    event(1, 'agent_output_chunk', 'server started\n'),
    event(2, 'agent_output_chunk', 'server ready\n'),
  ];

  async function rebootstrapped(transport: HistoryTransport): Promise<SocketSessionController> {
    const controller = new SocketSessionController(transport);
    await controller.start();
    transport.emitBatch(preAttach, 0, 'bootstrap-store');
    transport.emitBatch(runLog, 0, 'run-store');
    return controller;
  }

  it('reaches the state a full replay of the run log would have built', async () => {
    const replayedTransport = new HistoryTransport(runLog);
    const replayed = new SocketSessionController(replayedTransport);
    await replayed.start();
    replayedTransport.emitBatch(runLog, 0, 'run-store');

    const controller = await rebootstrapped(new HistoryTransport(runLog));

    expect(controller.state.core.transcript).toEqual(replayed.state.core.transcript);
    expect(controller.state.core.rounds).toEqual(replayed.state.core.rounds);
    expect(controller.state.core.sequence).toBe(replayed.state.core.sequence);
  });

  it('folds the run log prefix its stale cursor covered', async () => {
    const controller = await rebootstrapped(new HistoryTransport(runLog));

    // `run_started` is sequence 1 in the attached log and sequence 1 was
    // already folded from the log it replaces, so the out-of-order guard drops
    // it unless the batch is recognized as superseding what came before.
    expect(controller.state.core.maxRounds).toBe(3);
    expect(controller.state.core.outerLoop).toBe('agent');
    expect(controller.state.core.rounds.map(round => round.number)).toEqual([1]);
  });

  it('declares a complete history, so nothing is left to backfill', async () => {
    const transport = new HistoryTransport(runLog);

    const controller = await rebootstrapped(transport);

    expect(controller.state.core.historyAfterSequence).toBe(0);
    await expect(controller.loadOlderHistory()).resolves.toBe(false);
    expect(eventsQueries(transport)).toEqual([]);
  });

  it('extends rather than re-folds while the store stays the same', async () => {
    const transport = new HistoryTransport(runLog);
    const controller = new SocketSessionController(transport);
    await controller.start();

    transport.emitBatch(runLog.slice(0, 2), 0, 'run-store');
    transport.emitBatch(runLog.slice(2), 0, 'run-store');

    // A fresh run attaches its (empty) log without renumbering anything, so
    // its stream keeps one identity and the client must not discard the
    // events it already folded under it.
    expect(controller.state.core.maxRounds).toBe(3);
    expect(controller.state.core.rounds.map(round => round.number)).toEqual([1]);
    expect(controller.state.core.transcript.map(item => item.content).join('')).toContain('two\n');
  });
});

/**
 * The mirror of a raised floor: a fresh batch declaring a floor the stream has
 * already declared. `#declaredFloor` is the highest such floor, not the latest,
 * so a descent within one store is extra history rather than a gap, and the
 * climb back to a floor the stream already declared costs no refold. #1036.
 */
describe('a stream that re-declares a floor it already declared', () => {
  it('keeps the fold when the declared floor returns to an earlier level', async () => {
    const history = longHistory(2_000);
    const transport = new HistoryTransport(history);
    const controller = new SocketSessionController(transport);
    await controller.start();

    transport.emitBatch(history.slice(1_500), 1_500);
    expect(controller.state.core.historyAfterSequence).toBe(1_500);
    // A second bootstrap dial against the same store, floored lower: more
    // history offered and no gap, so the fold extends and the floor descends.
    transport.emitBatch(history.slice(1_000), 1_000);
    expect(controller.state.core.historyAfterSequence).toBe(1_000);
    const folded = controller.state.core.transcript.length;

    // Back at a floor the stream already declared: above the last one but not
    // above the highest, so there is no gap and the fold has to survive.
    transport.emitBatch([event(2_001, 'agent_output_chunk', 'live\n')], 1_500);

    expect(controller.state.core.historyAfterSequence).toBe(1_000);
    expect(controller.state.core.transcript).toHaveLength(folded + 1);
    expect(controller.state.core.transcript.at(-1)?.content).toBe('live\n');
  });

  it('takes a store change that lowers the floor as the new watermark', async () => {
    const transport = new HistoryTransport(longHistory(2_000));
    const controller = new SocketSessionController(transport);
    await controller.start();

    transport.emitBatch([event(1_501, 'agent_output_chunk', 'server\n')], 1_500, 'server-store');
    transport.emitBatch([event(301, 'agent_output_chunk', 'run\n')], 300, 'run-store');
    expect(controller.state.core.historyAfterSequence).toBe(300);

    // 900 is below the superseded store's watermark and above the attached
    // log's, so it is a burst outrunning the tail bound in the log now
    // streaming, not a return to a floor this stream declared.
    transport.emitBatch([event(901, 'agent_output_chunk', 'burst\n')], 900, 'run-store');

    expect(controller.state.core.historyAfterSequence).toBe(900);
    expect(controller.state.core.transcript.map(item => item.content)).toEqual(['burst\n']);
  });
});

/**
 * A backfill request is addressed in the sequence numbering of the log that
 * was streaming when it left. If the stream re-bootstraps before the answer
 * lands, the answer describes the superseded log and must be dropped rather
 * than folded under the fresh one.
 */
describe('a backfill in flight across a re-bootstrap', () => {
  it('drops a stale response when the store changed while it was in flight', async () => {
    const staleLog = longHistory(1_200);
    const transport = new HistoryTransport(staleLog);
    const controller = new SocketSessionController(transport);
    await controller.start();
    transport.emitBatch(staleLog.slice(1_100), 1_100, 'bootstrap-store');
    transport.deferEvents = true;

    const backfill = controller.loadOlderHistory();
    // The run's durable log attaches while the request is in flight.
    const runLog: RunEvent[] = [
      {
        ...event(1, 'run_started'),
        data: {kind: 'run_started', outer_loop: 'agent', input: '.', max_rounds: 3},
      },
      event(2, 'agent_output_chunk', 'fresh\n'),
    ];
    transport.emitBatch(runLog, 0, 'run-store');
    const fresh = controller.state.core.transcript;

    transport.releaseEvents();

    await expect(backfill).resolves.toBe(false);
    // The superseded log's chunk must not splice under the fresh fold.
    expect(controller.state.core.transcript).toBe(fresh);
    expect(controller.state.core.historyAfterSequence).toBe(0);
  });

  it('drops a stale response when the floor was raised while it was in flight', async () => {
    const history = longHistory(2_000);
    const transport = new HistoryTransport(history);
    const controller = new SocketSessionController(transport);
    await controller.start();
    transport.emitBatch(history.slice(1_500), 1_500);
    transport.deferEvents = true;

    const backfill = controller.loadOlderHistory();
    // A burst outran the tail bound, so the stream re-bootstrapped at a raised
    // floor within the same store.
    transport.emitBatch(history.slice(1_900), 1_900);
    transport.releaseEvents();

    await expect(backfill).resolves.toBe(false);
    // The response was numbered against the old floor; folding it would have
    // dragged the fresh floor down to its own range.
    expect(controller.state.core.historyAfterSequence).toBe(1_900);

    // The skipped history stays backfillable, one chunk under the new floor.
    transport.deferEvents = false;
    await expect(controller.loadOlderHistory()).resolves.toBe(true);
    expect(controller.state.core.historyAfterSequence).toBe(900);
    expect(controller.state.core.transcript).toHaveLength(1_100);
  });
});

describe('stream reconnect', () => {
  /** Lets the zero-delay reconnect timer and its subscribe settle. */
  const settle = () => new Promise<void>(resolve => setTimeout(resolve, 1));

  class ManualScheduler {
    readonly #pending: Array<() => void> = [];

    readonly scheduleTimeout: ScheduleTimeout = callback => {
      this.#pending.push(callback);
      return () => undefined;
    };

    get pendingCount(): number {
      return this.#pending.length;
    }

    runNext(): void {
      const callback = this.#pending.shift();
      if (callback === undefined) throw new Error('No reconnect is scheduled');
      callback();
    }
  }

  // The reconnect loop itself lives in and is tested against
  // PersistentEventStream; this checks only that the controller honors the
  // resumed tag it hands back, since the history floor is the controller's to
  // keep.
  it('keeps the history floor a resumed stream cannot vouch for', async () => {
    const transport = new ReconnectTransport();
    const controller = new SocketSessionController(transport, undefined, undefined, [0]);
    await controller.start();
    const experimentRequests = transport.requests.filter(
      request => request.type === 'query.experiments',
    ).length;
    // A tail bootstrap: everything at or below sequence 5 is unread history.
    transport.emitBatch([event(6, 'agent_output_chunk', 'six\n')], 5);
    expect(controller.state.core.historyAfterSequence).toBe(5);

    transport.sever();
    await settle();
    expect(transport.requests.filter(request => request.type === 'query.experiments')).toHaveLength(
      experimentRequests + 1,
    );
    // The resumed stream declares no floor of its own; taking its 0 literally
    // would claim the unread history below 5 is already loaded.
    transport.emitBatch([event(7, 'agent_output_chunk', 'seven\n')], 0);

    expect(controller.state.core.sequence).toBe(7);
    expect(controller.state.core.historyAfterSequence).toBe(5);
  });

  it('replaces the floor for a marked rebootstrap inside a resumed stream', async () => {
    const transport = new ReconnectTransport();
    const controller = new SocketSessionController(transport, undefined, undefined, [0]);
    await controller.start();
    transport.emitBatch([event(6, 'agent_output_chunk', 'six\n')], 5, 'run-store');
    expect(controller.state.core.historyAfterSequence).toBe(5);

    transport.sever();
    await settle();
    // The reconnect itself is a continuation, but the same socket then sends
    // a tail-overflow bootstrap. Keeping the dial-level `resumed` tag here
    // would preserve floor 5 and let scrollback address the superseded fold.
    transport.emitBatch([event(20, 'agent_output_chunk', 'twenty\n')], 19, 'run-store', true);

    expect(controller.state.core.sequence).toBe(20);
    expect(controller.state.core.historyAfterSequence).toBe(19);
  });

  it('re-bootstraps when the store was swapped while the stream was severed', async () => {
    const transport = new ReconnectTransport();
    const controller = new SocketSessionController(transport, undefined, undefined, [0]);
    await controller.start();
    // Boot against the bootstrap store: sequences 1 and 2 are folded from it.
    transport.emitBatch(
      [
        event(1, 'agent_output_chunk', 'server started\n'),
        event(2, 'agent_output_chunk', 'server ready\n'),
      ],
      0,
      'bootstrap-store',
    );
    expect(controller.state.core.sequence).toBe(2);

    transport.sever();
    await settle();
    // While severed, the durable log was attached: a new store whose sequence 1
    // is `run_started`, not the output the client folded at 1 in the old store.
    transport.emitBatch(
      [
        {
          ...event(1, 'run_started'),
          data: {kind: 'run_started', outer_loop: 'agent', input: '.', max_rounds: 3},
        },
        event(2, 'agent_output_chunk', 'two\n'),
      ],
      0,
      'run-store',
    );

    // The resume named the store the client last saw, and the batch that named
    // a different one superseded the fold: `run_started` at sequence 1 lands
    // even though sequence 1 was already folded, which the extend path drops.
    expect(transport.subscribeCalls.at(-1)?.storeId).toBe('bootstrap-store');
    expect(controller.state.core.maxRounds).toBe(3);
    expect(controller.state.core.outerLoop).toBe('agent');
    expect(controller.state.core.historyAfterSequence).toBe(0);
  });

  it('keeps the fold when an old-server fallback omits store identity', async () => {
    const transport = new ReconnectTransport();
    const controller = new SocketSessionController(transport, undefined, undefined, [0]);
    await controller.start();
    transport.emitBatch(
      [
        {
          ...event(1, 'run_started'),
          data: {kind: 'run_started', outer_loop: 'agent', input: '.', max_rounds: 3},
        },
      ],
      0,
      'run-store',
    );

    // A server predating store-aware resumes rejects the first dial, then the
    // stream falls back to a plain cursor resume whose suffix names no store.
    transport.refuseSubscribes = 1;
    transport.sever();
    await settle();
    expect(transport.subscribeCalls.at(-1)?.storeId).toBeUndefined();
    transport.emitBatch([event(2, 'agent_output_chunk', 'two\n')]);

    expect(controller.state.core.maxRounds).toBe(3);
    expect(controller.state.core.sequence).toBe(2);
    expect(controller.state.core.transcript.map(item => item.content).join('')).toContain('two\n');
  });

  it('recovers terminal boot history until the event fold reaches the snapshot watermark', async () => {
    const transport = new ReconnectTransport();
    const scheduler = new ManualScheduler();
    transport.snapshotStatus = 'completed';
    transport.snapshotSequence = 3;
    transport.deferSnapshot = true;
    const controller = new SocketSessionController(
      transport,
      undefined,
      undefined,
      [0],
      scheduler.scheduleTimeout,
    );

    const started = controller.start();
    // A valid bootstrap lands before the concurrent snapshot, but only carries
    // the first of the three events the snapshot says are durable.
    transport.emitBatch([event(1, 'agent_output_chunk', 'one\n')]);
    transport.releaseSnapshot();
    await started;
    expect(controller.state.core).toMatchObject({sequence: 1, status: 'completed'});

    transport.sever();

    expect(controller.state.eventStreamAvailable).toBe(false);
    expect(controller.state.errorBanner).toMatchObject({scope: 'transport'});
    expect(scheduler.pendingCount).toBe(1);

    const reconnected = new Promise<void>(resolve => {
      let unsubscribe: (() => void) | null = null;
      unsubscribe = controller.subscribe(state => {
        if (!state.eventStreamAvailable || transport.subscribeCalls.length !== 2) return;
        unsubscribe?.();
        resolve();
      });
    });
    scheduler.runNext();
    await reconnected;
    transport.emitBatch([
      event(2, 'agent_output_chunk', 'two\n'),
      event(3, 'agent_output_chunk', 'three\n'),
    ]);
    expect(controller.state.core.sequence).toBe(3);

    // Once the fold reaches the terminal snapshot's watermark, an ordinary
    // close is expected cleanup again: no banner and no third subscription.
    transport.sever();
    expect(scheduler.pendingCount).toBe(0);
    expect(controller.state.eventStreamAvailable).toBe(true);
    expect(controller.state.errorBanner).toBeNull();
    expect(transport.subscribeCalls).toHaveLength(2);
    await controller.stop();
  });

  it('recovers when a higher terminal snapshot resolves after the stream already closed', async () => {
    const transport = new ReconnectTransport();
    const scheduler = new ManualScheduler();
    transport.snapshotStatus = 'completed';
    transport.snapshotSequence = 3;
    transport.deferSnapshot = true;
    const controller = new SocketSessionController(
      transport,
      undefined,
      undefined,
      [0],
      scheduler.scheduleTimeout,
    );
    const availability: boolean[] = [];
    controller.subscribe(state => availability.push(state.eventStreamAvailable));

    const started = controller.start();
    transport.emitBatch([event(1, 'run_finished')]);
    transport.sever();
    expect(controller.state.core).toMatchObject({sequence: 1, status: 'completed'});
    expect(transport.subscribeCalls).toHaveLength(1);
    expect(scheduler.pendingCount).toBe(0);

    const reconnected = new Promise<void>(resolve => {
      let unsubscribe: (() => void) | null = null;
      unsubscribe = controller.subscribe(state => {
        if (!state.eventStreamAvailable || transport.subscribeCalls.length !== 2) return;
        unsubscribe?.();
        resolve();
      });
    });
    transport.releaseSnapshot();
    await started;
    await reconnected;

    expect(availability).toContain(false);
    expect(transport.subscribeCalls).toHaveLength(2);
    transport.emitBatch([event(2, 'agent_output_chunk', 'two\n'), event(3, 'run_finished')]);
    transport.sever();
    expect(scheduler.pendingCount).toBe(0);
    expect(controller.state.eventStreamAvailable).toBe(true);
    await controller.stop();
  });

  it('retires an in-flight snapshot watermark when a same-run store replaces its sequence space', async () => {
    const transport = new ReconnectTransport();
    const scheduler = new ManualScheduler();
    transport.snapshotStatus = 'completed';
    transport.snapshotSequence = 1_501;
    transport.deferSnapshot = true;
    const controller = new SocketSessionController(
      transport,
      undefined,
      undefined,
      [0],
      scheduler.scheduleTimeout,
    );

    const started = controller.start();
    transport.emitBatch([{...event(1_501, 'run_finished'), run_id: 'run'}], 1_500, 'server-store');
    // The replacement carries the same run id but numbers a different, shorter
    // store. Its terminal cursor is complete in that store's generation.
    transport.emitBatch([{...event(301, 'run_finished'), run_id: 'run'}], 300, 'run-store');
    expect(controller.state.core).toMatchObject({runId: 'run', sequence: 301, status: 'completed'});

    // This answer started before the rebootstrap and therefore cannot make a
    // claim about the new store's numeric tail, despite its matching run id.
    transport.releaseSnapshot();
    await started;
    expect(controller.state.core.sequence).toBe(301);

    transport.sever();
    expect(scheduler.pendingCount).toBe(0);
    expect(controller.state.eventStreamAvailable).toBe(true);
    expect(controller.state.errorBanner).toBeNull();
    expect(transport.subscribeCalls).toHaveLength(1);
    await controller.stop();
  });

  it.each<RunStatus>([
    'completed',
    'failed',
    'stopped',
  ])('keeps a %s replay quiet when its fold already reaches the snapshot watermark', async status => {
    const transport = new ReconnectTransport();
    const scheduler = new ManualScheduler();
    transport.snapshotStatus = status;
    transport.snapshotSequence = 1;
    transport.deferSnapshot = true;
    const controller = new SocketSessionController(
      transport,
      undefined,
      undefined,
      [0],
      scheduler.scheduleTimeout,
    );

    const started = controller.start();
    transport.emitBatch([event(1, 'agent_output_chunk', 'complete\n')]);
    transport.releaseSnapshot();
    await started;
    transport.sever();

    expect(scheduler.pendingCount).toBe(0);
    expect(controller.state.eventStreamAvailable).toBe(true);
    expect(controller.state.errorBanner).toBeNull();
    expect(transport.subscribeCalls).toHaveLength(1);
    await controller.stop();
  });

  /**
   * The #1044 regression on the TUI side. The controller already reports a
   * dropped stream as a transport banner without asking the run's status, so
   * the fix is entirely in `PersistentEventStream`: the report used to be
   * withheld before it ever reached `#onConnectionState`.
   */
  it('raises a transport banner when the stream faults on a run already ended', async () => {
    const transport = new ReconnectTransport();
    transport.snapshotStatus = 'completed';
    const controller = new SocketSessionController(transport, undefined, undefined, [0]);
    await controller.start();
    // The snapshot set a terminal status and folded nothing, so the banner
    // below is the only thing separating an empty transcript from a complete
    // one.
    expect(controller.state.core.status).toBe('completed');
    expect(controller.state.core.transcript).toEqual([]);
    expect(controller.state.errorBanner).toBeNull();

    transport.sever('Invalid event batch message');
    await settle();

    expect(controller.state.eventStreamAvailable).toBe(false);
    expect(controller.state.errorBanner).toMatchObject({scope: 'transport'});
    // The redial policy for an ended run is unchanged: the boot dial and
    // nothing after it.
    expect(transport.subscribeCalls).toHaveLength(1);
    await controller.stop();
  });

  it('reconnects after a fault when the snapshot carries a future run status', async () => {
    const transport = new ReconnectTransport();
    const scheduler = new ManualScheduler();
    transport.snapshotStatus = 'waiting_for_capacity' as RunStatus;
    const controller = new SocketSessionController(
      transport,
      undefined,
      undefined,
      [0],
      scheduler.scheduleTimeout,
    );
    await controller.start();
    const resumed = transport.nextSubscription();

    transport.sever('Invalid event batch message');

    expect(controller.state.eventStreamAvailable).toBe(false);
    expect(controller.state.errorBanner).toMatchObject({scope: 'transport'});
    expect(scheduler.pendingCount).toBe(1);

    scheduler.runNext();
    await resumed;
    expect(transport.subscribeCalls).toHaveLength(2);
    await controller.stop();
  });

  /**
   * The ordering the gateway actually produces when a subscription cannot be
   * built: `stream_failed` goes out before `subscribed`, then the handler
   * returns and the socket closes, so the close lands with no batch folded and
   * the stream reports it. The close is that failure's consequence, so the
   * specific diagnostic has to outlive it.
   */
  it('keeps a server protocol error when the close it caused is reported after it', async () => {
    const transport = new ReconnectTransport();
    const controller = new SocketSessionController(transport, undefined, undefined, [0]);
    await controller.start();

    transport.emit({
      type: 'protocol_error',
      code: 'stream_failed',
      message: 'Event stream failed: the run store is unreadable',
    } as ServerMessage);
    expect(controller.state.errorBanner).toMatchObject({
      scope: 'protocol',
      message: 'Event stream failed: the run store is unreadable',
    });

    transport.sever('Server event stream disconnected');
    await settle();

    expect(controller.state.errorBanner).toMatchObject({
      scope: 'protocol',
      message: 'Event stream failed: the run store is unreadable',
    });
    expect(controller.state.eventStreamAvailable).toBe(false);
    expect(transport.subscribeCalls).toHaveLength(1);
    await controller.stop();
  });

  it('keeps the fold and learns an identity first seen on a resumed suffix', async () => {
    const transport = new ReconnectTransport();
    const controller = new SocketSessionController(transport, undefined, undefined, [0, 0]);
    await controller.start();
    transport.emitBatch([
      {
        ...event(1, 'run_started'),
        data: {kind: 'run_started', outer_loop: 'agent', input: '.', max_rounds: 3},
      },
    ]);

    transport.sever();
    await settle();
    transport.emitBatch([event(2, 'agent_output_chunk', 'two\n')], 0, 'run-store');

    expect(controller.state.core.maxRounds).toBe(3);
    expect(controller.state.core.sequence).toBe(2);
    expect(controller.state.core.transcript.map(item => item.content).join('')).toContain('two\n');

    // Once learned, the identity protects the next cursor resume.
    transport.sever();
    await settle();
    expect(transport.subscribeCalls.at(-1)?.storeId).toBe('run-store');
  });
});

function iterationPage(
  revision: string,
  generation: number,
): Exclude<ProtocolResponse['iterations'], undefined | null> {
  return {
    kind: 'page',
    schema_version: 1,
    run_id: 'run',
    projection_revision: revision,
    parent_id: null,
    from_cursor: null,
    items: [
      {
        id: `generation:${generation}`,
        parent_id: null,
        kind: 'generation',
        label: `Generation ${generation}`,
        ordinal: generation,
        status: 'completed',
        child_count: 0,
        headline_metrics: [],
      },
    ],
    next_cursor: null,
  };
}

function iterationPageResponse(
  revision: string,
  fromCursor: string | null,
  start: number,
  count: number,
  nextCursor: string | null,
): ProtocolResponse {
  return {
    protocol_version: 1,
    request_id: `iterations-${revision}-${fromCursor ?? 'first'}`,
    timestamp: '2026-01-01T00:00:00Z',
    ok: true,
    iterations_ready: true,
    iterations: {
      kind: 'page',
      schema_version: 1,
      run_id: 'run',
      projection_revision: revision,
      parent_id: null,
      from_cursor: fromCursor,
      items: Array.from({length: count}, (_, offset) => {
        const generation = start + offset;
        return {
          id: `generation:${generation}`,
          parent_id: null,
          kind: 'generation' as const,
          label: `Generation ${generation}`,
          ordinal: generation,
          status: 'completed' as const,
          child_count: 0,
          headline_metrics: [],
        };
      }),
      next_cursor: nextCursor,
    },
  };
}

function deferred<T>(): {
  promise: Promise<T>;
  resolve(value: T): void;
  reject(error: unknown): void;
} {
  let resolvePromise: ((value: T) => void) | undefined;
  let rejectPromise: ((error: unknown) => void) | undefined;
  const promise = new Promise<T>((resolve, reject) => {
    resolvePromise = resolve;
    rejectPromise = reject;
  });
  return {
    promise,
    resolve(value) {
      if (resolvePromise === undefined) throw new Error('deferred promise is not initialized');
      resolvePromise(value);
    },
    reject(error) {
      if (rejectPromise === undefined) throw new Error('deferred promise is not initialized');
      rejectPromise(error);
    },
  };
}

async function settleMicrotasks(): Promise<void> {
  for (let turn = 0; turn < 10; turn += 1) await Promise.resolve();
}

function waitForController(
  controller: SocketSessionController,
  predicate: (state: typeof controller.state) => boolean,
): Promise<void> {
  if (predicate(controller.state)) return Promise.resolve();
  return new Promise(resolve => {
    const unsubscribe = controller.subscribe(state => {
      if (!predicate(state)) return;
      unsubscribe();
      resolve();
    });
  });
}

class FakeTransport implements ServerTransport {
  closed = false;
  closeCalls = 0;
  deferSubscribe = false;
  deferSnapshot = false;
  /** Mutable so a test can change what a refetch returns mid-run. */
  experiments: NonNullable<ProtocolResponse['experiments']> = [];
  experimentsReady = true;
  design: NonNullable<ProtocolResponse['design']> = [];
  designReady = true;
  iterations: Exclude<ProtocolResponse['iterations'], undefined> = null;
  iterationsReady = false;
  readonly iterationResponses: Array<ProtocolResponse | Error | Promise<ProtocolResponse>> = [];
  /** Patch text `query.design_patch` echoes back; null omits the field. */
  designPatchText: string | null = null;
  designPatchTruncated = false;
  /** When set, only `query.design_patch` requests fail with it. */
  designPatchError: Error | null = null;
  projectScope: string | undefined = NOTE_PROJECT_SCOPE;
  readonly requests: RequestInput[] = [];
  #message: ((message: ServerMessage) => void) | null = null;
  #disconnect: ((error: Error) => void) | null = null;
  #releaseSnapshot: (() => void) | null = null;
  #rejectSubscribe: ((error: Error) => void) | null = null;

  constructor(
    private readonly responseEvents: RunEvent[] = [],
    private readonly responsePerformance: NonNullable<ProtocolResponse['performance']> = [],
    private readonly responseChat?: NonNullable<ProtocolResponse['chat']>,
    private readonly responseError?: Error,
  ) {}

  #raiseConfiguredError(input: RequestInput): void {
    if (this.responseError) throw this.responseError;
    if (input.type === 'query.design_patch' && this.designPatchError) {
      throw this.designPatchError;
    }
  }

  #nextIterationResponse(
    input: RequestInput,
  ): ProtocolResponse | Promise<ProtocolResponse> | undefined {
    if (input.type !== 'query.iterations' || this.iterationResponses.length === 0) return undefined;
    const response = this.iterationResponses.shift();
    if (response instanceof Error) throw response;
    return response;
  }

  async request(input: RequestInput): Promise<ProtocolResponse> {
    this.requests.push(input);
    this.#raiseConfiguredError(input);
    const iterationResponse = this.#nextIterationResponse(input);
    if (iterationResponse !== undefined) return await iterationResponse;
    const response: ProtocolResponse = {
      protocol_version: 1,
      request_id: 'request',
      timestamp: '2026-01-01T00:00:00Z',
      ok: true,
      events: this.responseEvents,
      performance: this.responsePerformance,
      experiments: this.experiments,
      experiments_ready: this.experimentsReady,
      design: this.design,
      design_ready: this.designReady,
      ...(input.type === 'query.iterations'
        ? {iterations: this.iterations, iterations_ready: this.iterationsReady}
        : {}),
      ...(input.type === 'query.chat'
        ? {
            chat: this.responseChat ?? {
              question: input.text,
              answer: 'The implementer is running.',
              effect: 'none' as const,
            },
          }
        : {}),
      // Echoing the request triple is what the real server does, so tests
      // only choose the text; a null text is a detached server's answer.
      ...(input.type === 'query.design_patch' && this.designPatchText !== null
        ? {
            design_patch: {
              base: input.base,
              head: input.head,
              path: input.path,
              patch: this.designPatchText,
              truncated: this.designPatchTruncated,
            },
          }
        : {}),
      snapshot: {
        run_id: 'run',
        status: 'running',
        sequence: 12,
        ...(this.projectScope === undefined ? {} : {project_scope: this.projectScope}),
      },
    };
    if (input.type !== 'query.snapshot' || !this.deferSnapshot) return response;
    if (this.#releaseSnapshot !== null) throw new Error('Snapshot request is already pending');
    return new Promise(resolve => {
      this.#releaseSnapshot = () => {
        this.#releaseSnapshot = null;
        resolve(response);
      };
    });
  }

  subscribe(
    _afterSequence: number,
    onMessage: (message: ServerMessage) => void,
    onDisconnect: (error: Error) => void,
  ): Promise<EventSubscription> {
    this.#message = onMessage;
    this.#disconnect = onDisconnect;
    // The gateway sends `subscribed` and then exactly one bootstrap
    // `event_batch`, empty when the run has no history yet
    // (`_write_bootstrap`). Faithful because the stream reads it: a drop before
    // the bootstrap means the client holds none of the history it asked for,
    // which is a reported fault rather than a socket closing behind a fold that
    // is already complete.
    if (this.deferSubscribe) {
      return new Promise((_resolve, reject) => {
        this.#rejectSubscribe = reject;
      });
    }
    onMessage({type: 'event_batch', events: [], history_after_sequence: 0});
    return Promise.resolve({close: async () => undefined});
  }

  close(): Promise<void> {
    this.closeCalls += 1;
    this.closed = true;
    const reject = this.#rejectSubscribe;
    this.#rejectSubscribe = null;
    reject?.(new BackendClientError('disconnected', 'Client closed during subscription'));
    return Promise.resolve();
  }

  emit(message: ServerMessage): void {
    this.#message?.(message);
  }

  disconnect(error: Error): void {
    this.#disconnect?.(error);
  }

  releaseSnapshot(): void {
    const release = this.#releaseSnapshot;
    if (release === null) throw new Error('No snapshot request is pending');
    release();
  }
}

/**
 * A backend whose stream a test can sever and whose dials it can refuse:
 * everything the reconnect path needs in order to be observed.
 *
 * Unlike `FakeTransport`, the bootstrap batch is the test's to emit, because
 * where it lands relative to the sever is what these tests vary: a sever before
 * the first `emitBatch` is a socket that died between `subscribed` and its
 * history, which the stream treats differently from one that closed behind a
 * fold it had already delivered.
 */
class ReconnectTransport implements ServerTransport {
  readonly requests: RequestInput[] = [];
  /** Every subscribe: the cursor, tail, and store it carried, in order. */
  readonly subscribeCalls: Array<{
    afterSequence: number;
    tail: number | undefined;
    storeId: string | undefined;
  }> = [];
  /** How many upcoming subscribes the server refuses (a typed rejection). */
  refuseSubscribes = 0;
  /**
   * The status `query.snapshot` reports. A terminal one is what attaching to a
   * run after it finished gives, and the snapshot carries no events, so it sets
   * the status without folding anything.
   */
  snapshotStatus: RunStatus = 'running';
  snapshotSequence = 0;
  /** Hold `query.snapshot` so tests can order its answer against the bootstrap. */
  deferSnapshot = false;
  #message: ((message: ServerMessage) => void) | null = null;
  #disconnect: ((error: Error) => void) | null = null;
  #releaseSnapshot: (() => void) | null = null;
  readonly #subscriptionWaiters: Array<() => void> = [];

  request(input: RequestInput): Promise<ProtocolResponse> {
    this.requests.push(input);
    const response: ProtocolResponse = {
      protocol_version: 1,
      request_id: 'request',
      timestamp: '2026-01-01T00:00:00Z',
      ok: true,
      ...(input.type === 'query.experiments' ? {experiments: [], experiments_ready: true} : {}),
      ...(input.type === 'query.snapshot'
        ? {
            snapshot: {
              run_id: 'run',
              status: this.snapshotStatus,
              sequence: this.snapshotSequence,
            },
          }
        : {}),
    };
    if (input.type !== 'query.snapshot' || !this.deferSnapshot) return Promise.resolve(response);
    if (this.#releaseSnapshot !== null) throw new Error('Snapshot request is already pending');
    return new Promise(resolve => {
      this.#releaseSnapshot = () => {
        this.#releaseSnapshot = null;
        resolve(response);
      };
    });
  }

  subscribe(
    afterSequence: number,
    onMessage: (message: ServerMessage) => void,
    onDisconnect: (error: Error) => void,
    options?: SubscribeOptions,
  ): Promise<EventSubscription> {
    this.subscribeCalls.push({afterSequence, tail: options?.tail, storeId: options?.storeId});
    this.#subscriptionWaiters.shift()?.();
    if (this.refuseSubscribes > 0) {
      this.refuseSubscribes -= 1;
      return Promise.reject(new ServerError('Extra inputs are not permitted: store_id'));
    }
    this.#message = onMessage;
    this.#disconnect = onDisconnect;
    return Promise.resolve({close: async () => undefined});
  }

  /** Any stream message, for tests whose subject is where it lands. */
  emit(message: ServerMessage): void {
    this.#message?.(message);
  }

  emitBatch(
    events: readonly RunEvent[],
    historyAfterSequence = 0,
    storeId?: string,
    rebootstrap?: boolean,
  ): void {
    this.#message?.({
      type: 'event_batch',
      events: [...events],
      history_after_sequence: historyAfterSequence,
      ...(storeId === undefined ? {} : {store_id: storeId}),
      ...(rebootstrap === undefined ? {} : {rebootstrap}),
    });
  }

  sever(message = 'Server event stream disconnected'): void {
    this.#disconnect?.(new Error(message));
  }

  releaseSnapshot(): void {
    const release = this.#releaseSnapshot;
    if (release === null) throw new Error('No snapshot request is pending');
    release();
  }

  /** Resolves when the stream makes its next dial. */
  nextSubscription(): Promise<void> {
    return new Promise(resolve => this.#subscriptionWaiters.push(resolve));
  }

  close(): Promise<void> {
    return Promise.resolve();
  }
}

class DeferredExperimentsTransport implements ServerTransport {
  experimentRequests = 0;
  readonly #pending: Array<(response: ProtocolResponse) => void> = [];
  #message: ((message: ServerMessage) => void) | null = null;

  request(input: RequestInput): Promise<ProtocolResponse> {
    const base = {
      protocol_version: 1 as const,
      request_id: 'request',
      timestamp: '2026-01-01T00:00:00Z',
      ok: true,
    };
    if (input.type === 'query.snapshot') {
      return Promise.resolve({
        ...base,
        snapshot: {run_id: 'run', status: 'running', sequence: 0},
      });
    }
    if (input.type !== 'query.experiments') return Promise.resolve(base);
    this.experimentRequests += 1;
    if (this.experimentRequests === 1) {
      return Promise.resolve({...base, experiments: [], experiments_ready: true});
    }
    return new Promise(resolve => this.#pending.push(resolve));
  }

  resolveExperiment(experiments: NonNullable<ProtocolResponse['experiments']>): void {
    const resolve = this.#pending.shift();
    if (!resolve) throw new Error('No pending experiment request');
    resolve({
      protocol_version: 1,
      request_id: 'request',
      timestamp: '2026-01-01T00:00:00Z',
      ok: true,
      experiments,
      experiments_ready: true,
    });
  }

  subscribe(
    _afterSequence: number,
    onMessage: (message: ServerMessage) => void,
    _onDisconnect: (error: Error) => void,
  ): Promise<EventSubscription> {
    this.#message = onMessage;
    return Promise.resolve({close: async () => undefined});
  }

  emit(runEvent: RunEvent): void {
    this.#message?.({type: 'event', event: runEvent});
  }

  close(): Promise<void> {
    return Promise.resolve();
  }
}

class RevisionedExperimentsTransport implements ServerTransport {
  readonly experimentInputs: RequestInput[] = [];
  readonly #pending: Array<(response: ProtocolResponse) => void> = [];
  #message: ((message: ServerMessage) => void) | null = null;

  constructor(private readonly initial: NonNullable<ProtocolResponse['experiments']>) {}

  request(input: RequestInput): Promise<ProtocolResponse> {
    const base = {
      protocol_version: 1 as const,
      request_id: 'request',
      timestamp: '2026-01-01T00:00:00Z',
      ok: true,
    };
    if (input.type === 'query.snapshot') {
      return Promise.resolve({
        ...base,
        snapshot: {run_id: 'run', status: 'running', sequence: 0},
      });
    }
    if (input.type !== 'query.experiments') return Promise.resolve(base);
    this.experimentInputs.push(input);
    if (this.experimentInputs.length === 1) {
      return Promise.resolve({
        ...base,
        experiments: this.initial,
        experiments_ready: true,
        experiment_update: {
          run_id: 'run',
          projection_id: 'projection',
          through_revision: 1,
          reset: true,
        },
      });
    }
    return new Promise(resolve => this.#pending.push(resolve));
  }

  resolveExperiment(
    experiments: NonNullable<ProtocolResponse['experiments']>,
    update: NonNullable<ProtocolResponse['experiment_update']>,
  ): void {
    const resolve = this.#pending.shift();
    if (!resolve) throw new Error('No pending experiment request');
    resolve({
      protocol_version: 1,
      request_id: 'request',
      timestamp: '2026-01-01T00:00:00Z',
      ok: true,
      experiments,
      experiments_ready: true,
      experiment_update: update,
    });
  }

  subscribe(
    _afterSequence: number,
    onMessage: (message: ServerMessage) => void,
    _onDisconnect: (error: Error) => void,
  ): Promise<EventSubscription> {
    this.#message = onMessage;
    return Promise.resolve({close: async () => undefined});
  }

  emitExperimentChange(sequence: number, revision: number): void {
    this.#message?.({
      type: 'event',
      event: {
        sequence,
        run_id: 'run',
        timestamp: '2026-01-01T00:00:00Z',
        type: 'experiments_changed',
        data: {kind: 'experiments_changed', reason: 'round_persisted', revision},
      },
    });
  }

  emitProjectAttached(sequence: number): void {
    this.#message?.({
      type: 'event',
      event: {
        sequence,
        run_id: 'run',
        timestamp: '2026-01-01T00:00:00Z',
        type: 'experiments_changed',
        data: {kind: 'experiments_changed', reason: 'project_attached'},
      },
    });
  }

  close(): Promise<void> {
    return Promise.resolve();
  }
}

/** Holds experiment and design answers so invalidations can cross both requests exactly. */
class InvalidatedDesignTransport implements ServerTransport {
  design: NonNullable<ProtocolResponse['design']> = [];
  experimentRequests = 0;
  designRequests = 0;
  readonly #pendingExperiments: Array<{
    readonly response: Promise<ProtocolResponse>;
    readonly resolve: (response: ProtocolResponse) => void;
  }> = [];
  readonly #pendingDesign: Array<{
    readonly response: Promise<ProtocolResponse>;
    readonly resolve: (response: ProtocolResponse) => void;
    readonly reject: (error: Error) => void;
    readonly value: ProtocolResponse;
  }> = [];
  #message: ((message: ServerMessage) => void) | null = null;

  request(input: RequestInput): Promise<ProtocolResponse> {
    const base = {
      protocol_version: 1 as const,
      request_id: 'request',
      timestamp: '2026-01-01T00:00:00Z',
      ok: true as const,
    };
    if (input.type === 'query.snapshot') {
      return Promise.resolve({...base, snapshot: {run_id: 'run', status: 'running', sequence: 0}});
    }
    if (input.type === 'query.experiments') {
      this.experimentRequests += 1;
      if (this.experimentRequests === 1) {
        return Promise.resolve({...base, experiments: [], experiments_ready: true});
      }
      let resolve!: (response: ProtocolResponse) => void;
      const response = new Promise<ProtocolResponse>(done => {
        resolve = done;
      });
      this.#pendingExperiments.push({response, resolve});
      return response;
    }
    if (input.type === 'query.design') {
      this.designRequests += 1;
      const value = {...base, design: [...this.design], design_ready: true};
      let resolve!: (response: ProtocolResponse) => void;
      let reject!: (error: Error) => void;
      const response = new Promise<ProtocolResponse>((done, fail) => {
        resolve = done;
        reject = fail;
      });
      this.#pendingDesign.push({response, resolve, reject, value});
      return response;
    }
    return Promise.resolve(base);
  }

  async resolveExperiments(): Promise<void> {
    const pending = this.#pendingExperiments.shift();
    if (pending === undefined) throw new Error('No pending experiment request');
    pending.resolve({
      protocol_version: 1,
      request_id: 'request',
      timestamp: '2026-01-01T00:00:00Z',
      ok: true,
      experiments: [],
      experiments_ready: true,
    });
    await pending.response;
  }

  async settleDesign(outcome: 'success' | 'failure'): Promise<void> {
    const pending = this.#pendingDesign.shift();
    if (pending === undefined) throw new Error('No pending design request');
    if (outcome === 'success') {
      pending.resolve(pending.value);
      await pending.response;
      return;
    }
    const rejected = pending.response.catch(() => undefined);
    pending.reject(new Error('design query failed'));
    await rejected;
  }

  subscribe(
    _afterSequence: number,
    onMessage: (message: ServerMessage) => void,
    _onDisconnect: (error: Error) => void,
  ): Promise<EventSubscription> {
    this.#message = onMessage;
    return Promise.resolve({close: async () => undefined});
  }

  emitExperimentChange(sequence: number): void {
    this.#message?.({
      type: 'event',
      event: {
        sequence,
        run_id: 'run',
        timestamp: '2026-01-01T00:00:00Z',
        type: 'experiments_changed',
        data: {kind: 'experiments_changed', reason: 'round_persisted', revision: sequence},
      },
    });
  }

  close(): Promise<void> {
    return Promise.resolve();
  }
}

/** Holds both design-query call sites so their publication order is explicit. */
class CrossOwnedDesignTransport implements ServerTransport {
  design: NonNullable<ProtocolResponse['design']> = [];
  designRequests = 0;
  readonly #pendingDesign: Array<{
    readonly response: Promise<ProtocolResponse>;
    readonly resolve: (response: ProtocolResponse) => void;
    readonly reject: (error: Error) => void;
    readonly value: ProtocolResponse;
  }> = [];
  #message: ((message: ServerMessage) => void) | null = null;

  request(input: RequestInput): Promise<ProtocolResponse> {
    const base = {
      protocol_version: 1 as const,
      request_id: 'request',
      timestamp: '2026-01-01T00:00:00Z',
      ok: true as const,
    };
    if (input.type === 'query.snapshot') {
      return Promise.resolve({...base, snapshot: {run_id: 'run', status: 'running', sequence: 0}});
    }
    if (input.type === 'query.experiments') {
      return Promise.resolve({...base, experiments: [], experiments_ready: true});
    }
    if (input.type === 'query.performance') {
      return Promise.resolve({...base, performance: [], events: []});
    }
    if (input.type !== 'query.design') return Promise.resolve(base);
    this.designRequests += 1;
    const value = {...base, design: [...this.design], design_ready: true};
    let resolve!: (response: ProtocolResponse) => void;
    let reject!: (error: Error) => void;
    const response = new Promise<ProtocolResponse>((done, fail) => {
      resolve = done;
      reject = fail;
    });
    this.#pendingDesign.push({response, resolve, reject, value});
    return response;
  }

  async settleDesign(outcome: 'success' | 'failure'): Promise<void> {
    const pending = this.#pendingDesign.shift();
    if (pending === undefined) throw new Error('No pending design request');
    if (outcome === 'success') pending.resolve(pending.value);
    else pending.reject(new Error('design query failed'));
    await pending.response.catch(() => undefined);
    await settlePromiseEffects();
  }

  subscribe(
    _afterSequence: number,
    onMessage: (message: ServerMessage) => void,
    _onDisconnect: (error: Error) => void,
  ): Promise<EventSubscription> {
    this.#message = onMessage;
    onMessage({type: 'event_batch', events: [], history_after_sequence: 0});
    return Promise.resolve({close: async () => undefined});
  }

  emitExperimentChange(revision: number): void {
    this.#message?.({
      type: 'event',
      event: {
        sequence: revision,
        run_id: 'run',
        timestamp: '2026-01-01T00:00:00Z',
        type: 'experiments_changed',
        data: {kind: 'experiments_changed', reason: 'round_persisted', revision},
      },
    });
  }

  close(): Promise<void> {
    return Promise.resolve();
  }
}

/**
 * A backend whose design query the test holds open, so a slow /design can
 * overlap the next pane command the way the live git-backed query does.
 * Everything else answers immediately, including the perf query.
 */
class DeferredDesignTransport implements ServerTransport {
  readonly requests: RequestInput[] = [];
  /** Holds `query.design` answers until the test releases them. */
  deferDesign = false;
  readonly #pendingDesign: Array<() => void> = [];
  #message: ((message: ServerMessage) => void) | null = null;

  request(input: RequestInput): Promise<ProtocolResponse> {
    this.requests.push(input);
    const base = {
      protocol_version: 1 as const,
      request_id: 'request',
      timestamp: '2026-01-01T00:00:00Z',
      ok: true as const,
    };
    if (input.type === 'query.snapshot') {
      return Promise.resolve({...base, snapshot: {run_id: 'run', status: 'running', sequence: 0}});
    }
    if (input.type === 'query.experiments') {
      return Promise.resolve({...base, experiments: [], experiments_ready: true});
    }
    if (input.type === 'query.performance') {
      return Promise.resolve({
        ...base,
        performance: [
          {
            round: 1,
            perf_metric: 1200,
            perf_unit: 'total_ops_per_sec',
            passed: true,
            profile_skipped: false,
          },
          {
            round: 2,
            perf_metric: 2400,
            perf_unit: 'total_ops_per_sec',
            passed: true,
            profile_skipped: false,
          },
        ],
        events: [],
      });
    }
    if (input.type === 'query.design') {
      const response = {...base, design: [], design_ready: true};
      if (!this.deferDesign) return Promise.resolve(response);
      return new Promise(resolve => this.#pendingDesign.push(() => resolve(response)));
    }
    return Promise.resolve(base);
  }

  releaseDesign(): void {
    const pending = this.#pendingDesign.shift();
    if (!pending) throw new Error('No pending design request');
    pending();
  }

  subscribe(
    _afterSequence: number,
    onMessage: (message: ServerMessage) => void,
    _onDisconnect: (error: Error) => void,
  ): Promise<EventSubscription> {
    this.#message = onMessage;
    return Promise.resolve({close: async () => undefined});
  }

  emit(runEvent: RunEvent): void {
    this.#message?.({type: 'event', event: runEvent});
  }

  close(): Promise<void> {
    return Promise.resolve();
  }
}

class DeferredChatTransport implements ServerTransport {
  readonly requests: RequestInput[] = [];
  readonly #pending: Array<(response: ProtocolResponse) => void> = [];

  request(input: RequestInput): Promise<ProtocolResponse> {
    this.requests.push(input);
    return new Promise(resolve => this.#pending.push(resolve));
  }

  resolveNext(answer: string): void {
    const resolve = this.#pending.shift();
    if (!resolve) throw new Error('No pending chat request');
    resolve({
      protocol_version: 1,
      request_id: 'request',
      timestamp: '2026-01-01T00:00:00Z',
      ok: true,
      chat: {
        question: '',
        answer,
        effect: 'none',
      },
    });
  }

  subscribe(
    _afterSequence: number,
    _onMessage: (message: ServerMessage) => void,
    _onDisconnect: (error: Error) => void,
  ): Promise<EventSubscription> {
    return Promise.resolve({close: async () => undefined});
  }

  close(): Promise<void> {
    return Promise.resolve();
  }
}

/** A backend that creates one thread and answers threaded chat. */
class ThreadTransport implements ServerTransport {
  readonly requests: RequestInput[] = [];
  #sequence = 0;
  #threads = 0;
  /** Providers and models the backend says this run offers. */
  chatOptions: NonNullable<ProtocolResponse['chat_options']> | null = {
    providers: [
      {
        provider: 'codex',
        models: [
          {model: 'gpt-run', source: 'run', default: true},
          {model: 'gpt-5.6-sol', source: 'suggested', default: false},
        ],
      },
      {provider: 'claude', models: [{model: 'claude-opus-5', source: 'suggested', default: false}]},
    ],
  };

  request(input: RequestInput): Promise<ProtocolResponse> {
    this.requests.push(input);
    const base = {
      protocol_version: 1 as const,
      request_id: 'request',
      timestamp: '2026-01-01T00:00:00Z',
      ok: true,
    };
    if (input.type === 'query.snapshot') {
      return Promise.resolve({...base, snapshot: {run_id: 'run', status: 'running', sequence: 0}});
    }
    if (input.type === 'query.experiments') {
      return Promise.resolve({...base, experiments: [], experiments_ready: true});
    }
    if (input.type === 'query.chat_options') {
      return Promise.resolve({...base, chat_options: this.chatOptions});
    }
    if (input.type === 'query.chat_thread_create') {
      const threadId = `thread-${++this.#threads}`;
      const settings = {
        provider: input.provider ?? 'codex',
        model: input.model ?? 'gpt-run',
      };
      return Promise.resolve({
        ...base,
        chat_thread: {thread_id: threadId, title: '', ...settings},
        events: [
          {
            sequence: ++this.#sequence,
            timestamp: '2026-01-01T00:00:01Z',
            type: 'chat_thread_created' as const,
            agent_kind: 'chat',
            round_label: 'experiment-chat',
            chat_thread_id: threadId,
            data: {
              kind: 'chat_thread_created' as const,
              thread_id: threadId,
              title: '',
              ...settings,
              created_at: '2026-01-01T00:00:01Z',
            },
          },
        ],
      });
    }
    if (input.type === 'query.chat') {
      const threadId = input.thread_id ?? null;
      return Promise.resolve({
        ...base,
        chat: {question: input.text, answer: 'Thread answer.', effect: 'none' as const},
        events: [
          {
            sequence: ++this.#sequence,
            timestamp: '2026-01-01T00:00:02Z',
            type: 'chat' as const,
            agent_kind: 'chat',
            round_label: 'experiment-chat',
            text: input.text,
            ...(threadId === null ? {} : {chat_thread_id: threadId}),
            data: {kind: 'chat' as const, answer: 'Thread answer.'},
          },
        ],
      });
    }
    return Promise.resolve(base);
  }

  subscribe(
    _afterSequence: number,
    _onMessage: (message: ServerMessage) => void,
    _onDisconnect: (error: Error) => void,
  ): Promise<EventSubscription> {
    return Promise.resolve({close: async () => undefined});
  }

  close(): Promise<void> {
    return Promise.resolve();
  }
}

/** Thread-create replies settle only when the test chooses, in any order. */
class DeferredThreadCreateTransport extends ThreadTransport {
  rejectCreatesOnClose: Error | null = null;
  readonly #pending: Array<{
    input: Extract<RequestInput, {type: 'query.chat_thread_create'}>;
    resolve: (response: ProtocolResponse) => void;
    reject: (error: Error) => void;
  }> = [];

  override request(input: RequestInput): Promise<ProtocolResponse> {
    if (input.type !== 'query.chat_thread_create') return super.request(input);
    this.requests.push(input);
    return new Promise((resolve, reject) => this.#pending.push({input, resolve, reject}));
  }

  resolveCreate(index: number, threadId: string, sequence: number): void {
    const pending = this.#pending[index];
    if (pending === undefined) throw new Error(`No pending thread-create request ${index}`);
    const settings = {
      provider: pending.input.provider ?? 'codex',
      model: pending.input.model ?? 'gpt-run',
    };
    pending.resolve({
      protocol_version: 1,
      request_id: `request-${index}`,
      timestamp: '2026-01-01T00:00:01Z',
      ok: true,
      chat_thread: {thread_id: threadId, title: '', ...settings},
      events: [
        {
          sequence,
          timestamp: '2026-01-01T00:00:01Z',
          type: 'chat_thread_created',
          agent_kind: 'chat',
          round_label: 'experiment-chat',
          chat_thread_id: threadId,
          data: {
            kind: 'chat_thread_created',
            thread_id: threadId,
            title: '',
            ...settings,
            created_at: '2026-01-01T00:00:01Z',
          },
        },
      ],
    });
  }

  rejectCreate(index: number, error: Error): void {
    const pending = this.#pending[index];
    if (pending === undefined) throw new Error(`No pending thread-create request ${index}`);
    pending.reject(error);
  }

  override close(): Promise<void> {
    const error = this.rejectCreatesOnClose;
    if (error !== null) {
      for (const pending of this.#pending) pending.reject(error);
    }
    return Promise.resolve();
  }
}

/** Chat options stay under test control while the rest of the transport behaves normally. */
class DeferredChatOptionsTransport extends ThreadTransport {
  readonly #pending: Array<{
    resolve: (response: ProtocolResponse) => void;
    reject: (error: Error) => void;
  }> = [];

  override request(input: RequestInput): Promise<ProtocolResponse> {
    if (input.type !== 'query.chat_options') return super.request(input);
    this.requests.push(input);
    return new Promise((resolve, reject) => this.#pending.push({resolve, reject}));
  }

  resolveNext(model: string): void {
    const pending = this.#pending.shift();
    if (pending === undefined) throw new Error('No pending chat-options request');
    pending.resolve({
      protocol_version: 1,
      request_id: 'request',
      timestamp: '2026-01-01T00:00:00Z',
      ok: true,
      chat_options: {
        providers: [
          {
            provider: 'codex',
            models: [{model, source: 'suggested', default: false}],
          },
        ],
      },
    });
  }

  rejectNext(error: Error): void {
    const pending = this.#pending.shift();
    if (pending === undefined) throw new Error('No pending chat-options request');
    pending.reject(error);
  }
}

/**
 * A tail-capable backend holding one run's whole history. The subscription
 * delivers whatever a test emits, and `query.events` answers out of the same
 * history, so a backfill returns exactly the range the controller asked for.
 */
class HistoryTransport implements ServerTransport {
  readonly requests: RequestInput[] = [];
  /** The `tail` each subscribe carried, in order; `undefined` for a plain one. */
  readonly subscribeTails: Array<number | undefined> = [];
  /** Rejects a subscribe carrying `tail`, the way a server without the field does. */
  rejectTail = false;
  /** Fails every subscribe that `rejectTail` did not already reject. */
  subscribeError: Error | null = null;
  /** Fails `query.events` instead of answering it. */
  eventsError: Error | null = null;
  /** Holds `query.events` answers until the test releases them. */
  deferEvents = false;
  readonly #pendingEvents: Array<() => void> = [];
  #message: ((message: ServerMessage) => void) | null = null;

  constructor(private readonly history: readonly RunEvent[] = []) {}

  request(input: RequestInput): Promise<ProtocolResponse> {
    this.requests.push(input);
    const base = {
      protocol_version: 1 as const,
      request_id: 'request',
      timestamp: '2026-01-01T00:00:00Z',
      ok: true as const,
    };
    if (input.type !== 'query.events') return Promise.resolve(base);
    if (this.eventsError !== null) return Promise.reject(this.eventsError);
    const after = input.after_sequence ?? 0;
    const before = input.before_sequence ?? Number.MAX_SAFE_INTEGER;
    const response = {
      ...base,
      events: this.history.filter(
        item => (item.sequence ?? 0) > after && (item.sequence ?? 0) < before,
      ),
    };
    if (!this.deferEvents) return Promise.resolve(response);
    return new Promise(resolve => this.#pendingEvents.push(() => resolve(response)));
  }

  subscribe(
    _afterSequence: number,
    onMessage: (message: ServerMessage) => void,
    _onDisconnect: (error: Error) => void,
    options?: SubscribeOptions,
  ): Promise<EventSubscription> {
    this.subscribeTails.push(options?.tail);
    if (this.rejectTail && options?.tail !== undefined) {
      return Promise.reject(new ServerError('Extra inputs are not permitted: tail'));
    }
    if (this.subscribeError !== null) return Promise.reject(this.subscribeError);
    this.#message = onMessage;
    return Promise.resolve({close: async () => undefined});
  }

  /** One bootstrap batch: the spine below the floor, then the tail. */
  emitBatch(events: readonly RunEvent[], historyAfterSequence: number, storeId?: string): void {
    this.#message?.({
      type: 'event_batch',
      events: [...events],
      history_after_sequence: historyAfterSequence,
      // Omitted rather than empty when a test does not care, so the default
      // path stays what a server that reports no store identity sends.
      ...(storeId === undefined ? {} : {store_id: storeId}),
    });
  }

  releaseEvents(): void {
    const pending = this.#pendingEvents.shift();
    if (!pending) throw new Error('No pending query.events request');
    pending();
  }

  close(): Promise<void> {
    return Promise.resolve();
  }
}

function eventsQueries(transport: HistoryTransport): RequestInput[] {
  return transport.requests.filter(request => request.type === 'query.events');
}

function longHistory(length: number): RunEvent[] {
  return Array.from({length}, (_, index) =>
    event(index + 1, 'agent_output_chunk', `event ${index + 1}\n`),
  );
}

function roundFinished(sequence: number, round: number): RunEvent {
  return {
    sequence,
    timestamp: '2026-01-01T00:00:00Z',
    type: 'round_finished',
    round_label: `round-${round}`,
    data: {
      kind: 'round_finished',
      attempts: 1,
      judge_verdict: 'pass',
      profile_skipped: false,
    },
  };
}

function perfRequests(transport: FakeTransport): number {
  return transport.requests.filter(request => request.type === 'query.performance').length;
}

function patchRequests(transport: FakeTransport): RequestInput[] {
  return transport.requests.filter(request => request.type === 'query.design_patch');
}

function designQueries(transport: DeferredDesignTransport): number {
  return transport.requests.filter(request => request.type === 'query.design').length;
}

function designRound(round: number): NonNullable<ProtocolResponse['design']>[number] {
  return {
    round,
    base: `base-${round}`,
    commit: `commit-${round}`,
    files: [{path: `src/round-${round}.ts`, change: 'modified'}],
  };
}

/** Drain promise continuations only, without wall-clock time or scheduler ordering. */
async function settlePromiseEffects(): Promise<void> {
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
}

function entry(
  id: string,
  firstRound: number,
  lastRound: number,
  overrides: Partial<NonNullable<ProtocolResponse['experiments']>[number]> = {},
): NonNullable<ProtocolResponse['experiments']>[number] {
  return {
    hypothesis_id: id,
    identified: true,
    first_round: firstRound,
    last_round: lastRound,
    rounds: [],
    kept: false,
    active: false,
    ...overrides,
  };
}
