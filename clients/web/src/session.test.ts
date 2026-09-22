import {strict as assert} from 'node:assert';
import {test} from 'node:test';
import {
  type ProtocolResponse,
  type RequestInput,
  type RunEvent,
  ServerError,
  type ServerMessage,
  type SubscribeOptions,
} from '@vibesys/backend-client/browser';
import {type WorkspaceClient, WorkspaceSession} from './session.js';

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
  assert.deepEqual(session.getSnapshot().command, {sending: false, error: null});
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
    assert.deepEqual(session.getSnapshot().command, {sending: false, error: null});

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
    assert.deepEqual(state.command, {sending: false, error: null});
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
  data: {kind: 'round_finished', attempts: 1, judge_verdict: 'pass'},
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
  const performance = session.getSnapshot().queries.performance.response;
  assert.deepEqual(performance?.performance, [], 'the series is dropped');
  assert.equal(performance?.performance_context?.objective_baseline_value, 900);
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
    sending: false,
    error: {action: 'pause', message: 'The run is not running.'},
  });
  assert.equal(await session.command({type: 'command.steer', text: 'Keep going'}), true);
  assert.deepEqual(session.getSnapshot().command, {sending: false, error: null});
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
