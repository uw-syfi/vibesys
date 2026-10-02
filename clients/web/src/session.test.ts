import {strict as assert} from 'node:assert';
import {readFileSync} from 'node:fs';
import {test} from 'node:test';
import {
  BackendClientError,
  type ControlChannelState,
  type ProtocolResponse,
  type RequestInput,
  type RunEvent,
  ServerError,
  type ServerMessage,
  type SubscribeOptions,
  sameControlChannelState,
} from '@vibesys/backend-client';
import {replayTransport} from './replay.js';
import {
  type BrowserLifecycle,
  CAPTURED_TYPES,
  type WorkspaceClient,
  WorkspaceSession,
  type WorkspaceTransportHooks,
  webSocketUrlFromLocation,
} from './session.js';

const response = (fields: Partial<ProtocolResponse> = {}): ProtocolResponse => ({
  request_id: 'test',
  ok: true,
  ...fields,
});
const output = (sequence: number, text: string): RunEvent => ({
  sequence,
  type: 'agent_output_chunk',
  timestamp: '2026-09-21T12:00:00Z',
  data: {kind: 'agent_output_chunk', content: text, channel: 'assistant'},
});
const status = (sequence: number, value: 'running' | 'paused'): RunEvent => ({
  sequence,
  type: 'run_status_changed',
  timestamp: '2026-09-21T12:00:00Z',
  data: {kind: 'run_status_changed', status: value, previous: 'running'},
});

class FakeClient implements WorkspaceClient {
  runId = 'run-1';
  replay: ((after: number) => ServerMessage) | undefined;
  messages: ((message: ServerMessage) => void)[] = [];
  disconnect: ((error: Error) => void) | undefined;
  subscriptions: {after: number; options: SubscribeOptions | undefined}[] = [];
  requests: RequestInput[] = [];
  closed = false;
  closedSubscriptions = 0;
  refuseDials = false;
  withholdBatch = false;
  reconnectCalls = 0;
  hooks: WorkspaceTransportHooks | null = null;
  #controls: ControlChannelState = {status: 'connected'};
  replies: (input: RequestInput) => Promise<ProtocolResponse> = async input => {
    if (input.type === 'query.snapshot')
      return response({snapshot: {run_id: 'run-1', sequence: 0, status: 'starting'}});
    if (input.type === 'query.experiments')
      return response({experiments_ready: false, experiments: []});
    if (input.type === 'query.design') return response({design_ready: false, design: []});
    if (input.type === 'command.pause')
      return response({ack: {action: 'pause', status: 'pending'}});
    if (input.type === 'command.steer')
      return response({ack: {action: 'steer', status: 'pending'}});
    return response();
  };
  request(input: RequestInput): Promise<ProtocolResponse> {
    this.requests.push(input);
    return this.replies(input);
  }
  async subscribe(
    after: number,
    onMessage: (message: ServerMessage) => void,
    onDisconnect: (error: Error) => void,
    options?: SubscribeOptions,
  ) {
    this.messages.push(onMessage);
    this.disconnect = onDisconnect;
    this.subscriptions.push({after, options});
    if (this.refuseDials) throw new Error('dial refused');
    onMessage({type: 'subscribed', run_id: this.runId, request_id: 'sub', latest_sequence: 0});
    // Like the gateway, every subscription starts with one batch, empty or not.
    if (!this.withholdBatch) {
      onMessage(
        this.replay?.(after) ?? {
          type: 'event_batch',
          events: [],
          through_sequence: after,
          active_executions: [],
        },
      );
    }
    return {
      close: async () => {
        this.closedSubscriptions += 1;
      },
    };
  }
  emit(message: ServerMessage) {
    this.messages.at(-1)?.(message);
  }
  /**
   * The factory a session builds its transport through, so the control channel
   * has somewhere to report. Passing the instance instead leaves `controls` at
   * its starting value, which is what the other tests here want.
   */
  factory = (hooks: WorkspaceTransportHooks): WorkspaceClient => {
    this.hooks = hooks;
    return this;
  };
  /**
   * The control channel lost a connection it had and reported the outage.
   *
   * Deduplicated like the real `ControlChannel`: a report is emitted only when
   * something a consumer renders actually changed, so a Fake cannot let the
   * session get away with behavior the real channel never produces.
   */
  dropControlChannel(message: string, retrying = false) {
    this.#report({
      status: 'disconnected',
      error: new BackendClientError('disconnected', message),
      everConnected: true,
      retrying,
    });
  }
  /** A redial brought the control channel back. */
  recoverControlChannel() {
    this.#report({status: 'connected'});
  }
  /**
   * Redial now. The real one cancels an armed redial and dials; this one
   * recovers the channel and reports it, so a test asserts the affordance had
   * an effect rather than that a method was called.
   */
  reconnect() {
    this.reconnectCalls += 1;
    this.recoverControlChannel();
  }
  #report(state: ControlChannelState) {
    if (sameControlChannelState(this.#controls, state)) return;
    this.#controls = state;
    this.hooks?.onConnectionState(state);
  }
  async close() {
    this.closed = true;
  }
}
const settle = async () => {
  await new Promise(resolve => setTimeout(resolve, 0));
};

test('replays and deduplicates events; command acknowledgment never changes lifecycle', async () => {
  const client = new FakeClient();
  const session = new WorkspaceSession(client);
  await session.start();
  client.emit({
    type: 'event_batch',
    events: [status(1, 'running'), output(2, 'hello')],
    through_sequence: 2,
    active_executions: [],
  });
  client.emit({type: 'event', event: output(2, 'hello')});
  assert.equal(
    session.getSnapshot().core.transcript.filter(entry => entry.content === 'hello').length,
    1,
  );
  assert.equal(
    await session.command({type: 'command.pause', mode: 'after_current_agent_call'}),
    true,
  );
  assert.deepEqual(session.getSnapshot().command, {sending: null, error: null});
  assert.equal(session.getSnapshot().core.status, 'running');
  client.emit({type: 'event', event: status(3, 'paused')});
  assert.equal(session.getSnapshot().core.status, 'paused');
  await settle();
  assert.equal(
    session.getSnapshot().core.status,
    'paused',
    'late snapshot cannot roll back events',
  );
  await session.close();
  assert.equal(client.closed, true);
  assert.equal(client.closedSubscriptions, 1);
});

test('preserves unattached readiness, then refreshes on canonical experiment invalidation', async () => {
  const client = new FakeClient();
  const session = new WorkspaceSession(client);
  await session.start();
  assert.equal(session.getSnapshot().queries.experiments.response?.experiments_ready, false);
  assert.equal(session.getSnapshot().queries.design.response?.design_ready, false);
  const previous = client.replies;
  client.replies = async input =>
    input.type === 'query.experiments'
      ? response({
          experiments_ready: true,
          experiments: [
            {
              hypothesis_id: 'h1',
              first_round: 1,
              last_round: 1,
              rounds: [{round: 1, passed: true, reviewed: true, official_evaluation: false}],
            },
          ],
        })
      : previous(input);
  client.emit({
    type: 'event',
    event: {
      sequence: 10,
      type: 'experiments_changed',
      timestamp: '2026-09-21T12:00:00Z',
      data: {kind: 'experiments_changed', reason: 'project_attached'},
    },
  });
  await settle();
  assert.equal(session.getSnapshot().queries.experiments.response?.experiments_ready, true);
  assert.equal(
    session.getSnapshot().queries.experiments.response?.experiments?.[0]?.rounds?.[0]
      ?.official_evaluation,
    false,
  );
  await session.close();
});

test('backfill excludes tail spine duplicates and a raised floor rebuilds the projection', async () => {
  const client = new FakeClient();
  const session = new WorkspaceSession(client);
  await session.start();
  client.emit({
    type: 'event_batch',
    events: [output(1, 'spine'), output(10, 'tail')],
    history_after_sequence: 8,
    through_sequence: 10,
    active_executions: [],
  });
  const previous = client.replies;
  client.replies = async input =>
    input.type === 'query.events'
      ? response({events: [output(1, 'spine'), output(3, 'older')]})
      : previous(input);
  await session.loadOlder();
  assert.deepEqual(
    session.getSnapshot().core.transcript.map(entry => entry.content),
    ['spine', 'older', 'tail'],
  );
  assert.equal(session.getSnapshot().core.historyAfterSequence, 0);
  client.emit({
    type: 'event_batch',
    events: [output(11, 'live')],
    history_after_sequence: 8,
    through_sequence: 11,
  });
  assert.equal(
    session.getSnapshot().core.historyAfterSequence,
    0,
    'live batches cannot raise a backfilled floor',
  );
  client.emit({
    type: 'event_batch',
    events: [output(2, 'attached spine'), output(22, 'attached tail')],
    history_after_sequence: 20,
    through_sequence: 22,
    active_executions: [],
  });
  assert.deepEqual(
    session.getSnapshot().core.transcript.map(entry => entry.content),
    ['attached spine', 'attached tail'],
  );
  assert.equal(session.getSnapshot().core.historyAfterSequence, 20);
  await session.close();
});

test('a bootstrap that supersedes a backfill clears its loading flag and error', async () => {
  const client = new FakeClient();
  client.replay = () => ({
    type: 'event_batch',
    events: [],
    history_after_sequence: 700,
    through_sequence: 1000,
    active_executions: [],
  });
  const previous = client.replies;
  let release: (() => void) | undefined;
  client.replies = input => {
    if (input.type !== 'query.events') return previous(input);
    if (release !== undefined) return Promise.resolve(response({events: []}));
    return new Promise(resolve => {
      release = () => resolve(response({events: []}));
    });
  };
  const session = new WorkspaceSession(client);
  await session.start();
  const older = session.loadOlder();
  assert.equal(session.getSnapshot().historyLoading, true);
  await session.reconnect();
  release?.();
  await older;
  assert.equal(session.getSnapshot().historyLoading, false, 'the superseded chunk holds no lock');
  await session.loadOlder();
  assert.equal(session.getSnapshot().core.historyAfterSequence, 200);

  client.replies = async input =>
    input.type === 'query.events' ? Promise.reject(new Error('down')) : previous(input);
  await session.loadOlder();
  assert.equal(session.getSnapshot().historyError, 'down');
  await session.reconnect();
  assert.equal(session.getSnapshot().historyError, null, 'the rebuilt projection has no failure');
  await session.close();
});

test('automatic reconnect resumes the cursor and retains incomplete history', async () => {
  const client = new FakeClient();
  const session = new WorkspaceSession(client);
  await session.start();
  client.emit({
    type: 'event_batch',
    events: [status(10, 'running')],
    history_after_sequence: 8,
    through_sequence: 10,
    active_executions: [],
  });
  client.disconnect?.(new Error('offline'));
  assert.equal(session.getSnapshot().connection, 'disconnected');
  await new Promise(resolve => setTimeout(resolve, 550));
  assert.equal(client.subscriptions.at(-1)?.after, 10);
  assert.equal(client.subscriptions.at(-1)?.options, undefined);
  client.emit({
    type: 'event_batch',
    events: [output(11, 'resumed')],
    history_after_sequence: 0,
    through_sequence: 11,
    active_executions: [],
  });
  assert.equal(session.getSnapshot().core.historyAfterSequence, 8);
  assert.equal(session.getSnapshot().connection, 'connected');
  await session.close();
  client.messages[0]?.({type: 'event', event: output(99, 'late')});
  assert.equal(session.getSnapshot().core.sequence, 11);
});

test('a refresh during an outstanding query is not lost, and errors allow retries', async () => {
  const client = new FakeClient();
  const session = new WorkspaceSession(client);
  await session.start();
  let resolve: ((value: ProtocolResponse) => void) | undefined;
  const previous = client.replies;
  client.replies = input =>
    input.type === 'query.experiments'
      ? new Promise(done => {
          resolve = done;
        })
      : previous(input);
  const pending = session.load('experiments');
  client.emit({
    type: 'event',
    event: {sequence: 1, type: 'experiments_changed', timestamp: '2026-09-21T12:00:00Z'},
  });
  client.replies = async input =>
    input.type === 'query.experiments'
      ? response({experiments_ready: true, experiments: []})
      : previous(input);
  resolve?.(response({experiments_ready: false}));
  await pending;
  await settle();
  assert.equal(session.getSnapshot().queries.experiments.response?.experiments_ready, true);
  client.replies = async () => {
    throw new Error('request failed');
  };
  assert.equal(await session.command({type: 'command.steer', text: 'keep the baseline'}), false);
  assert.deepEqual(session.getSnapshot().command.error, {
    action: 'steer',
    message: 'request failed',
  });
  await session.load('design');
  assert.equal(session.getSnapshot().queries.design.error, 'request failed');
  client.replies = previous;
  await session.load('design');
  assert.equal(session.getSnapshot().queries.design.error, null);
  await session.close();
});

for (const failOldRequests of [false, true]) {
  test(`a new run reboots cursor 100 at zero and rejects late old-run ${failOldRequests ? 'errors' : 'results'}`, async () => {
    const client = new FakeClient();
    const session = new WorkspaceSession(client);
    await session.start();
    client.emit({
      type: 'event_batch',
      events: [output(1, 'old spine'), output(100, 'old evidence')],
      history_after_sequence: 90,
      through_sequence: 100,
    });
    await settle();
    await session.command({type: 'command.pause', mode: 'after_current_agent_call'});

    const pending: {
      input: RequestInput;
      resolve: (value: ProtocolResponse) => void;
      reject: (error: Error) => void;
    }[] = [];
    client.replies = input =>
      new Promise((resolve, reject) => pending.push({input, resolve, reject}));
    const oldRefresh = session.refresh();
    const oldHistory = session.loadOlder();
    const oldCommand = session.command({type: 'command.steer', text: 'old guidance'});
    client.runId = 'run-2';
    client.replies = async input => {
      if (input.type === 'query.snapshot')
        return response({snapshot: {run_id: 'run-2', sequence: 3, status: 'running'}});
      if (input.type === 'query.design')
        return response({
          design_ready: true,
          design: [{round: 1, files: [{path: 'new.ts', change: 'added'}]}],
        });
      return response({experiments_ready: true, experiments: []});
    };
    client.replay = after => ({
      type: 'event_batch',
      events:
        after === 0 ? [output(1, 'new one'), output(2, 'new two'), output(3, 'new three')] : [],
      through_sequence: 3,
      history_after_sequence: 0,
      active_executions: [],
    });
    client.disconnect?.(new Error('backend replaced'));
    await new Promise(resolve => setTimeout(resolve, 550));
    assert.deepEqual(
      client.subscriptions.map(item => item.after),
      [0, 100, 0],
    );
    assert.equal(session.getSnapshot().runId, 'run-2');
    assert.equal(session.getSnapshot().core.sequence, 3);
    assert.deepEqual(session.getSnapshot().command, {sending: null, error: null});

    for (const request of pending) {
      if (failOldRequests) request.reject(new Error('old run failed'));
      else
        request.resolve(
          response({
            snapshot: {run_id: 'run-1', sequence: 100, status: 'paused'},
            events: [output(50, 'late old history')],
            experiments_ready: false,
            design_ready: false,
            ack: {action: 'steer', status: 'pending'},
          }),
        );
    }
    await Promise.all([oldRefresh, oldHistory, oldCommand]);
    client.messages[1]?.({type: 'event', event: output(101, 'late old socket')});
    const state = session.getSnapshot();
    assert.equal(state.runId, 'run-2');
    assert.equal(state.core.sequence, 3);
    assert.equal(state.core.status, 'running');
    assert.equal(state.core.historyAfterSequence, 0);
    assert.deepEqual(
      state.core.transcript.map(entry => entry.content),
      ['new one', 'new two', 'new three'],
    );
    assert.equal(state.queries.experiments.response?.experiments_ready, true);
    assert.equal(state.queries.design.response?.design?.[0]?.files?.[0]?.path, 'new.ts');
    assert.equal(state.snapshotError, null);
    assert.equal(state.historyError, null);
    assert.deepEqual(state.command, {sending: null, error: null});
    for (const query of Object.values(state.queries)) assert.equal(query.error, null);
    await session.close();
  });
}

test('manual reconnect is single-flight and ignores messages from the replaced stream', async () => {
  const client = new FakeClient();
  const session = new WorkspaceSession(client);
  await session.start();
  await Promise.all([session.reconnect(), session.reconnect()]);
  assert.equal(client.subscriptions.length, 2);
  assert.equal(client.closedSubscriptions, 1);
  client.emit({type: 'event_batch', events: [output(2, 'current')], through_sequence: 2});
  client.messages[0]?.({type: 'event', event: output(100, 'stale')});
  assert.equal(session.getSnapshot().core.sequence, 2);
  await session.close();
  assert.equal(client.closedSubscriptions, 2);
});

test('protocol errors disable commands and remain visible until an explicit reconnect', async () => {
  const client = new FakeClient();
  const session = new WorkspaceSession(client);
  await session.start();
  client.emit({type: 'protocol_error', code: 'unsupported', message: 'Upgrade required'});
  assert.equal(session.getSnapshot().connection, 'error');
  assert.equal(session.getSnapshot().connectionError, 'unsupported: Upgrade required');
  assert.equal(await session.command({type: 'command.steer', text: 'Do not send'}), false);
  assert.equal(
    client.requests.some(request => request.type === 'command.steer'),
    false,
  );
  await session.reconnect();
  assert.equal(session.getSnapshot().connection, 'connected');
  assert.equal(session.getSnapshot().connectionError, null);
  await session.close();
});

const experimentsChanged = (sequence: number): RunEvent => ({
  sequence,
  type: 'experiments_changed',
  timestamp: '2026-09-21T12:00:00Z',
  data: {kind: 'experiments_changed', reason: 'round_persisted'},
});
const roundFinished = (sequence: number): RunEvent => ({
  sequence,
  type: 'round_finished',
  round_label: 'round-1',
  timestamp: '2026-09-21T12:00:00Z',
  data: {kind: 'round_finished', attempts: 1, judge_verdict: 'pass', profile_skipped: false},
});
const control = (sequence: number, text: string, value: 'pending' | 'consumed'): RunEvent => ({
  sequence,
  type: 'control',
  text,
  status: value,
  timestamp: '2026-09-21T12:00:00Z',
});
const queryCounts = (client: FakeClient): Record<string, number> => {
  const counts: Record<string, number> = {};
  for (const {type} of client.requests) {
    if (type?.startsWith('query.')) counts[type] = (counts[type] ?? 0) + 1;
  }
  return counts;
};

test('query budget: four queries per bootstrap, three per experiments_changed, none otherwise', async () => {
  const client = new FakeClient();
  client.replay = after => ({
    type: 'event_batch',
    events: after === 0 ? [status(1, 'running'), experimentsChanged(2), roundFinished(3)] : [],
    through_sequence: Math.max(after, 3),
    active_executions: [],
  });
  const previous = client.replies;
  client.replies = async input =>
    input.type === 'query.performance'
      ? response({
          performance: [{round: 1, perf_metric: 1000, perf_unit: 'ops', passed: true}],
          performance_context: {objective_description: 'Go fast.', objective_baseline_value: 900},
        })
      : previous(input);
  const session = new WorkspaceSession(client, {reconnectDelaysMs: [1]});
  await session.start();
  await settle();
  const bootstrap = {
    'query.snapshot': 1,
    'query.experiments': 1,
    'query.design': 1,
    'query.performance': 1,
  };
  assert.deepEqual(queryCounts(client), bootstrap, 'replayed invalidations are history');
  const series = () => session.getSnapshot().queries.performance.response;
  // The rail's sparkline plots the raw rows, so they are kept beside the context, not dropped.
  assert.deepEqual(series()?.performance, [
    {round: 1, perf_metric: 1000, perf_unit: 'ops', passed: true},
  ]);
  assert.equal(series()?.performance_context?.objective_baseline_value, 900);
  client.emit({type: 'event', event: roundFinished(4)});
  client.emit({type: 'event', event: status(5, 'paused')});
  await settle();
  assert.deepEqual(queryCounts(client), bootstrap, 'rounds and status changes query nothing');
  client.emit({type: 'event', event: experimentsChanged(6)});
  await settle();
  const invalidated = {
    'query.snapshot': 1,
    'query.experiments': 2,
    'query.design': 2,
    'query.performance': 2,
  };
  assert.deepEqual(queryCounts(client), invalidated);
  assert.equal(series()?.performance?.length, 1, 'the rows survive the refetch');
  client.disconnect?.(new Error('offline'));
  await new Promise(resolve => setTimeout(resolve, 20));
  assert.equal(client.subscriptions.at(-1)?.after, 6, 'the reconnect resumed the cursor');
  assert.deepEqual(queryCounts(client), invalidated, 'a resumed stream refetches nothing');
  await session.close();
});

test('query budget: a resumed batch with experiments_changed refetches three queries, no snapshot', async () => {
  const client = new FakeClient();
  client.replay = after => ({
    type: 'event_batch',
    events: after === 0 ? [status(1, 'running')] : [experimentsChanged(after + 1)],
    through_sequence: after === 0 ? 1 : after + 1,
    active_executions: [],
  });
  const session = new WorkspaceSession(client, {reconnectDelaysMs: [1]});
  await session.start();
  await settle();
  client.disconnect?.(new Error('offline'));
  await new Promise(resolve => setTimeout(resolve, 20));
  assert.equal(client.subscriptions.at(-1)?.after, 1, 'the reconnect resumed the cursor');
  await settle();
  assert.deepEqual(queryCounts(client), {
    'query.snapshot': 1,
    'query.experiments': 2,
    'query.design': 2,
    'query.performance': 2,
  });
  await session.close();
});

test('records control events core-state drops, deduplicated, and resets them with the projection', async () => {
  const client = new FakeClient();
  const session = new WorkspaceSession(client);
  await session.start();
  const texts = () =>
    session.getSnapshot().captured.map(event => `${event.sequence} ${event.text}`);
  client.emit({
    type: 'event_batch',
    events: [status(1, 'running'), control(2, '/steer: Keep the baseline', 'pending')],
    through_sequence: 2,
  });
  client.emit({type: 'event', event: control(2, '/steer: Keep the baseline', 'pending')});
  client.emit({type: 'event', event: control(3, '/steer', 'consumed')});
  assert.deepEqual(texts(), ['2 /steer: Keep the baseline', '3 /steer']);
  assert.equal(session.getSnapshot().core.transcript.length, 0, 'core-state keeps no trace');
  const held = session.getSnapshot().captured;
  client.emit({type: 'event', event: output(4, 'hello')});
  assert.equal(session.getSnapshot().captured, held, 'other events keep the array identity');
  client.emit({
    type: 'event_batch',
    events: [control(30, '/steer: After the attach', 'pending')],
    history_after_sequence: 20,
    through_sequence: 30,
  });
  assert.deepEqual(texts(), ['30 /steer: After the attach'], 'a raised floor rebuilds the record');
  const previous = client.replies;
  client.replies = async input =>
    input.type === 'query.events'
      ? response({events: [control(12, '/steer: Older', 'pending'), output(13, 'older')]})
      : previous(input);
  await session.loadOlder();
  assert.deepEqual(texts(), ['12 /steer: Older', '30 /steer: After the attach']);
  await session.close();
});

test('Retry appears only once the reconnect schedule is exhausted; an ended run never redials', async () => {
  const client = new FakeClient();
  const session = new WorkspaceSession(client, {reconnectDelaysMs: [1, 1]});
  await session.start();
  client.refuseDials = true;
  client.disconnect?.(new Error('offline'));
  assert.equal(session.getSnapshot().connection, 'disconnected');
  assert.equal(session.getSnapshot().canRetry, false, 'still reconnecting');
  await new Promise(resolve => setTimeout(resolve, 30));
  assert.equal(client.subscriptions.length, 3, 'the boot dial plus both scheduled dials');
  assert.equal(session.getSnapshot().canRetry, true);
  client.refuseDials = false;
  await session.reconnect();
  assert.equal(session.getSnapshot().connection, 'connected');
  assert.equal(session.getSnapshot().canRetry, false);
  client.emit({
    type: 'event',
    event: {
      sequence: 9,
      type: 'run_finished',
      status: 'completed',
      timestamp: '2026-09-21T12:00:00Z',
    },
  });
  const dials = client.subscriptions.length;
  client.disconnect?.(new Error('backend exited'));
  await new Promise(resolve => setTimeout(resolve, 30));
  assert.equal(client.subscriptions.length, dials, 'no redial after the run ended');
  assert.equal(session.getSnapshot().connection, 'connected', 'no banner for lifecycle cleanup');
  await session.close();
});

test('a failed command keeps its diagnostic summary until the next command', async () => {
  const client = new FakeClient();
  const session = new WorkspaceSession(client);
  await session.start();
  const previous = client.replies;
  client.replies = async input => {
    if (input.type === 'command.pause') {
      throw new ServerError('Pause rejected', {
        code: 'illegal_transition',
        summary: 'The run is not running.',
        scope: 'request',
      });
    }
    return previous(input);
  };
  assert.equal(
    await session.command({type: 'command.pause', mode: 'after_current_agent_call'}),
    false,
  );
  assert.deepEqual(session.getSnapshot().command, {
    sending: null,
    error: {action: 'pause', message: 'The run is not running.'},
  });
  assert.equal(await session.command({type: 'command.steer', text: 'Keep going'}), true);
  assert.deepEqual(session.getSnapshot().command, {sending: null, error: null});
  await session.close();
});

for (const reconnectDelaysMs of [[1], []]) {
  test(`Retry stays reachable when the stream drops before its first batch (${reconnectDelaysMs.length} scheduled dials)`, async () => {
    const client = new FakeClient();
    client.withholdBatch = true;
    const session = new WorkspaceSession(client, {reconnectDelaysMs});
    await session.start();
    assert.equal(session.getSnapshot().connection, 'connected');
    client.refuseDials = true;
    client.disconnect?.(new Error('offline'));
    await new Promise(resolve => setTimeout(resolve, 30));
    assert.equal(session.getSnapshot().connection, 'disconnected');
    assert.equal(session.getSnapshot().canRetry, true, 'the schedule is exhausted');
    await session.close();
  });
}

const ws = (href: string) => webSocketUrlFromLocation({href});

test('maps page and gateway capability URLs to the gateway WebSocket endpoint', () => {
  assert.equal(
    ws('http://localhost:4173/runs/demo?token=secret&unused=x'),
    'ws://localhost:4173/ws?token=secret',
  );
  assert.equal(
    ws('https://example.test/app?token=encoded%20token'),
    'wss://example.test/ws?token=encoded+token',
  );
  assert.equal(
    ws('http://127.0.0.1:5173/?gateway=http%3A%2F%2F127.0.0.1%3A8765%2F%3Ftoken%3Dsecret'),
    'ws://127.0.0.1:8765/ws?token=secret',
  );
  assert.equal(ws('https://127.0.0.1:8765/?token=secret'), 'wss://127.0.0.1:8765/ws?token=secret');
});

test('never forwards the page capability token to a foreign gateway authority', () => {
  assert.equal(
    ws('http://127.0.0.1:8765/?token=secret&gateway=http%3A%2F%2F127.0.0.1%3A5173%2F'),
    'ws://127.0.0.1:5173/ws?token=',
  );
});

test('sends a capability token only to the authority whose own URL carried it', () => {
  const pageOrigins = ['http://127.0.0.1:8765', 'https://gateway.test'];
  const gatewayValues = [
    null,
    '/',
    'http://127.0.0.1:5173/',
    'http://127.0.0.1:5173/?token=gateway-token',
    'https://elsewhere.test/',
    '//elsewhere.test/',
    'http://127.0.0.1:8765@elsewhere.test/',
  ];
  const cases = pageOrigins.flatMap(origin =>
    gatewayValues.flatMap(gateway =>
      [null, 'page-token'].map(pageToken => ({origin, gateway, pageToken})),
    ),
  );

  const results = cases.map(({origin, gateway, pageToken}) => {
    const page = new URL(origin);
    if (pageToken !== null) page.searchParams.set('token', pageToken);
    if (gateway !== null) page.searchParams.set('gateway', gateway);
    const socket = new URL(ws(page.href));
    return {
      sent: socket.searchParams.get('token'),
      pageAuthority: socket.origin === origin.replace(/^http/, 'ws'),
    };
  });

  assert.deepEqual(
    results.filter(result => result.sent === 'page-token' && !result.pageAuthority),
    [],
  );
  assert.ok(results.some(result => result.sent !== ''));
});

test('resumes with the store id, and folds a replaced store as a fresh bootstrap', async () => {
  const client = new FakeClient();
  let store = 'store-1';
  client.replay = after => ({
    type: 'event_batch',
    events: after === 0 || store === 'store-2' ? [output(1, store)] : [],
    through_sequence: 1,
    active_executions: [],
    history_after_sequence: 0,
    store_id: store,
  });
  const session = new WorkspaceSession(client, {reconnectDelaysMs: [1]});
  await session.start();
  store = 'store-2';
  client.disconnect?.(new Error('backend replaced'));
  await new Promise(resolve => setTimeout(resolve, 10));
  assert.equal(client.subscriptions[1]?.options?.storeId, 'store-1');
  const contents = session.getSnapshot().core.transcript.map(entry => entry.content);
  assert.ok(contents.includes('store-2'));
  assert.ok(!contents.includes('store-1'));
  await session.close();
});

test('a visible, online page redials a stream whose reconnect schedule gave up', async () => {
  const listeners = new Map<string, () => void>();
  const lifecycle: BrowserLifecycle & {visibilityState: DocumentVisibilityState} = {
    visibilityState: 'hidden',
    online: true,
    addEventListener: (type, listener) => listeners.set(type, listener),
    removeEventListener: type => listeners.delete(type),
  };
  const client = new FakeClient();
  const session = new WorkspaceSession(client, {reconnectDelaysMs: [], lifecycle});
  await session.start();
  client.disconnect?.(new Error('offline'));
  await settle();
  assert.equal(session.getSnapshot().connection, 'disconnected');
  listeners.get('visibilitychange')?.();
  await settle();
  assert.equal(client.subscriptions.length, 1, 'a hidden page does not redial');
  lifecycle.visibilityState = 'visible';
  listeners.get('visibilitychange')?.();
  await settle();
  assert.equal(client.subscriptions.length, 2);
  assert.equal(session.getSnapshot().connection, 'connected');
  await session.close();
  assert.equal(listeners.size, 0);
});

test('the replay transport folds the recorded run and refuses commands', async () => {
  const fixture = new URL('../../tui/dev/fixtures/framework-events.jsonl', import.meta.url);
  const transport = replayTransport(Promise.resolve(readFileSync(fixture, 'utf8')));
  const session = new WorkspaceSession(transport);
  await session.start();
  await settle();
  const {core} = session.getSnapshot();
  assert.equal(core.roundLabel, 'round-2');
  assert.ok(core.transcript.some(entry => entry.content === 'PASS'));
  await assert.rejects(transport.request({type: 'command.resume'}), /read-only/);
  await session.close();
});

test('captures agent executions and round results for the run view', () => {
  for (const type of [
    'agent_execution_started',
    'agent_execution_finished',
    'invocation_started',
    'round_finished',
  ]) {
    assert.ok(CAPTURED_TYPES.has(type), type);
  }
});

test('stop is sent as command.stop and reports its action while in flight', async () => {
  const client = new FakeClient();
  let release: (reply: ProtocolResponse) => void = () => {};
  client.replies = input =>
    input.type === 'command.stop'
      ? new Promise(resolve => {
          release = resolve;
        })
      : Promise.resolve(response());
  const session = new WorkspaceSession(client);
  await session.start();
  const sent = session.command({type: 'command.stop', mode: 'after_current_agent_call'});
  assert.equal(session.getSnapshot().command.sending, 'stop');
  release(response({ack: {action: 'stop', status: 'pending'}}));
  assert.equal(await sent, true);
  assert.deepEqual(client.requests.at(-1), {
    type: 'command.stop',
    mode: 'after_current_agent_call',
  });
  assert.deepEqual(session.getSnapshot().command, {sending: null, error: null});
});

test('acknowledged steers keep their own ids and the sequence they were sent after', async () => {
  const client = new FakeClient();
  const session = new WorkspaceSession(client);
  await session.start();
  client.emit({
    type: 'event_batch',
    events: [status(1, 'running'), output(2, 'hello')],
    through_sequence: 2,
    active_executions: [],
  });
  assert.equal(await session.command({type: 'command.steer', text: 'Go'}), true);
  assert.equal(await session.command({type: 'command.steer', text: 'Go'}), true);
  assert.deepEqual(session.getSnapshot().sent, [
    {id: 'sent-1', text: 'Go', afterSequence: 2},
    {id: 'sent-2', text: 'Go', afterSequence: 2},
  ]);
});

test('design patches are requested by range and path; an unattached server answers null', async () => {
  const client = new FakeClient();
  const session = new WorkspaceSession(client);
  await session.start();
  assert.equal(await session.designPatch('aaa', 'bbb', 'src/lib.rs'), null);
  assert.deepEqual(client.requests.at(-1), {
    type: 'query.design_patch',
    base: 'aaa',
    head: 'bbb',
    path: 'src/lib.rs',
  });
  client.replies = async () =>
    response({
      design_patch: {base: 'aaa', head: 'bbb', path: 'src/lib.rs', patch: '+x\n', truncated: false},
    });
  assert.equal((await session.designPatch('aaa', 'bbb', 'src/lib.rs'))?.patch, '+x\n');
});

const chatEvent = (
  sequence: number,
  question: string,
  answer: string,
  thread: string | null = null,
): RunEvent => ({
  sequence,
  type: 'chat',
  timestamp: '2026-09-21T12:00:00Z',
  text: question,
  status: 'answered',
  agent_kind: 'chat',
  round_label: 'experiment-chat',
  chat_thread_id: thread,
  data: {kind: 'chat', answer, invocation_id: `inv-${sequence}`},
});

test('chat events are captured for the Ask tab', () => {
  assert.ok(CAPTURED_TYPES.has('chat'));
});

test('one question per thread at a time; the stream and the response carry one answer once', async () => {
  const client = new FakeClient();
  let reply: (value: ProtocolResponse) => void = () => {};
  const previous = client.replies;
  client.replies = input =>
    input.type === 'query.chat'
      ? new Promise(resolve => {
          reply = resolve;
        })
      : previous(input);
  const session = new WorkspaceSession(client);
  await session.start();
  assert.equal(session.ask('Why did round 3 fail?', 'default'), true);
  assert.equal(session.ask('And round 4?', 'default'), false, 'the default thread waits');
  assert.equal(session.ask('Other thread', 't2'), true, 'another thread is free');
  assert.deepEqual(
    client.requests.filter(request => request.type === 'query.chat'),
    [
      {type: 'query.chat', text: 'Why did round 3 fail?'},
      {type: 'query.chat', text: 'Other thread', thread_id: 't2'},
    ],
  );
  assert.deepEqual(
    session.getSnapshot().asks.map(ask => [ask.id, ask.threadId, ask.afterSequence]),
    [
      ['ask-1', 'default', 0],
      ['ask-2', 't2', 0],
    ],
  );
  const event = chatEvent(5, 'Other thread', 'Answered.', 't2');
  // The backend publishes the recorded answer before it returns the response.
  client.emit({type: 'event', event});
  reply(response({chat: {question: 'Other thread', answer: 'Answered.'}, events: [event]}));
  await settle();
  const state = session.getSnapshot();
  assert.equal(state.captured.filter(item => item.type === 'chat').length, 1);
  assert.deepEqual(
    state.asks.map(ask => ask.id),
    ['ask-1'],
    'the answered ask leaves; the other still waits',
  );
  await session.close();
});

test('an unrecorded answer stays on its ask; a failure keeps its message; a new run drops asks', async () => {
  const client = new FakeClient();
  const previous = client.replies;
  client.replies = async input => {
    if (input.type === 'query.chat' && 'thread_id' in input && input.thread_id === 't2')
      return response({
        chat: {question: 'Hi', answer: 'Thread t2 cannot answer right now.', thread_id: 't2'},
        events: [],
      });
    if (input.type === 'query.chat') throw new Error('gateway closed');
    return previous(input);
  };
  const session = new WorkspaceSession(client);
  await session.start();
  session.ask('Hi', 't2');
  session.ask('Why?', 'default');
  await settle();
  assert.deepEqual(
    session.getSnapshot().asks.map(ask => [ask.threadId, ask.answer, ask.error]),
    [
      ['t2', 'Thread t2 cannot answer right now.', null],
      ['default', null, 'gateway closed'],
    ],
  );
  assert.equal(session.ask('Again', 'default'), true, 'a failed ask does not hold its thread');
  client.emit({type: 'subscribed', run_id: 'run-2', request_id: 'sub', latest_sequence: 0});
  assert.deepEqual(session.getSnapshot().asks, []);
  await session.close();
});

test('a question whose answer was recorded before the connection dropped is answered, not failed', async () => {
  const client = new FakeClient();
  let fail: (error: Error) => void = () => {};
  const previous = client.replies;
  client.replies = input =>
    input.type === 'query.chat'
      ? new Promise((_, reject) => {
          fail = reject;
        })
      : previous(input);
  const session = new WorkspaceSession(client);
  await session.start();
  session.ask('Why?', 'default');
  client.emit({type: 'event', event: chatEvent(5, 'Why?', 'Because.')});
  fail(new Error('Server disconnected during chat'));
  await settle();
  assert.deepEqual(session.getSnapshot().asks, []);
  await session.close();
});

test('threads are created with the chosen model, or the run default without one', async () => {
  const client = new FakeClient();
  const previous = client.replies;
  client.replies = async input =>
    input.type === 'query.chat_thread_create'
      ? response({
          chat_thread: {
            thread_id: 't9',
            driver: 'agentshim',
            provider: 'claude',
            model: 'claude-sonnet-5',
          },
        })
      : previous(input);
  const session = new WorkspaceSession(client);
  await session.start();
  assert.equal(await session.createThread({provider: 'claude', model: 'claude-sonnet-5'}), 't9');
  assert.deepEqual(client.requests.at(-1), {
    type: 'query.chat_thread_create',
    provider: 'claude',
    model: 'claude-sonnet-5',
  });
  await session.createThread(null);
  assert.deepEqual(client.requests.at(-1), {type: 'query.chat_thread_create'});
  client.replies = async () => response();
  await assert.rejects(session.createThread(null), /no chat thread/);
  await session.close();
});

test('chat options are loaded on demand, never by a bootstrap', async () => {
  const client = new FakeClient();
  const previous = client.replies;
  client.replies = async input =>
    input.type === 'query.chat_options'
      ? response({chat_options: {providers: [{provider: 'claude', models: []}]}})
      : previous(input);
  const session = new WorkspaceSession(client);
  await session.start();
  await settle();
  assert.equal(queryCounts(client)['query.chat_options'], undefined);
  assert.deepEqual(session.getSnapshot().queries.chat_options, {
    response: null,
    loading: false,
    error: null,
  });
  await session.load('chat_options');
  assert.equal(
    session.getSnapshot().queries.chat_options.response?.chat_options?.providers?.[0]?.provider,
    'claude',
  );
  await session.close();
});

test('a dead control channel is its own state, redialed without touching the stream', async () => {
  const client = new FakeClient();
  const session = new WorkspaceSession(client.factory);
  await session.start();
  assert.deepEqual(session.getSnapshot().controls, {status: 'connected'});
  const published: string[] = [];
  session.subscribe(() => published.push(session.getSnapshot().controls.status));

  client.dropControlChannel('gateway restarted');
  const dead = session.getSnapshot();
  // The transcript is still streaming; only the command path is down, and the
  // two are reported as the separate facts they are.
  assert.equal(dead.connection, 'connected');
  assert.equal(dead.connectionError, null);
  assert.deepEqual(dead.controls, {
    status: 'disconnected',
    error: new BackendClientError('disconnected', 'gateway restarted'),
    everConnected: true,
    retrying: false,
  });
  assert.deepEqual(published, ['disconnected']);

  // A dial starting is a change a frontend renders (the affordance goes dead
  // while `reconnect()` would no-op), so the dedup must not swallow it.
  client.dropControlChannel('gateway restarted', true);
  const retrying = session.getSnapshot().controls;
  assert.equal(retrying.status === 'disconnected' && retrying.retrying, true);

  const requests = client.requests.length;
  session.reconnectControls();
  // The affordance reaches the transport's redial verb and the recovery it
  // produces reaches the session's state. Issuing a request instead would only
  // queue behind the backoff the outage already armed.
  assert.equal(client.reconnectCalls, 1);
  assert.deepEqual(session.getSnapshot().controls, {status: 'connected'});
  // And it is the command path only: a live transcript is not resubscribed.
  await settle();
  assert.equal(client.subscriptions.length, 1);
  assert.equal(client.requests.length, requests);

  await session.close();
  client.dropControlChannel('too late');
  assert.deepEqual(session.getSnapshot().controls, {status: 'connected'});
  session.reconnectControls();
  assert.equal(client.reconnectCalls, 1);
});
