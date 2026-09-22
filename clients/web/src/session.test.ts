import {strict as assert} from 'node:assert';
import {test} from 'node:test';
import type {
  ProtocolResponse,
  RequestInput,
  RunEvent,
  ServerMessage,
  SubscribeOptions,
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
    onMessage({type: 'subscribed', run_id: this.runId, request_id: 'sub', latest_sequence: 0});
    if (this.replay) onMessage(this.replay(after));
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
  assert.equal(session.getSnapshot().command.ack?.status, 'pending');
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
  assert.equal(session.getSnapshot().command.error, 'request failed');
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
    assert.equal(session.getSnapshot().command.ack, null);

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
    assert.deepEqual(state.command, {sending: false, ack: null, error: null});
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
