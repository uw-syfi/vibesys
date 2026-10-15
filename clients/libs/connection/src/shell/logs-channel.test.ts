/**
 * `HostConnection.openLogs`: the host's one logs request channel, opened by the first request of
 * the first lease, shared by every lease, closed when the last lease is released, and never opened
 * for a helper that does not offer `logs` (`docs/contributing/desktop-connection.md`, "Logs").
 * An aborted read is withdrawn with `logs.cancel` when the helper offers `logs_cancel`.
 */
import {describe, expect, test} from 'bun:test';
import type {HelperRequest} from '@vibesys/contracts/host-helper';
import {type LogReadInput, startLogFollower} from '@vibesys/logs';
import {FakeClock} from '@vibesys/transport/testing';
import type {ConnectionConfig} from '../state/host-connection.js';
import {FakeLink} from '../testing/fake-helper.js';
import {HostConnection} from './host-connection.js';

const CONFIG: ConnectionConfig = {delaysMs: [500, 1_000], deadlineMs: 60_000, heartbeatMs: 15_000};
const INSTANCE = '0123456789ab';
const encoder = new TextEncoder();
const NEVER = new AbortController().signal;

function connectionOver(link: FakeLink, clock = new FakeClock()): HostConnection {
  return new HostConnection({
    link,
    checkout: '/src/vibesys',
    appVersion: 'test',
    config: CONFIG,
    scheduleTimeout: clock.scheduleTimeout,
    log: () => {},
  });
}

/** A read that waits at the end of the seeded stream, as a follower's long poll does. */
const WAITING_READ: LogReadInput = {
  instance_id: INSTANCE,
  stream_id: '000001',
  start: {offset: 27},
  max_bytes: 1024,
  wait_ms: 25_000,
};

function requestsOf<T extends HelperRequest['type']>(
  link: FakeLink,
  type: T,
): Extract<HelperRequest, {type: T}>[] {
  return link.helper.requests.filter(
    (request): request is Extract<HelperRequest, {type: T}> => request.type === type,
  );
}

/** A link whose helper's store holds one startup stream for `INSTANCE`. */
function seededLink(): FakeLink {
  const link = new FakeLink();
  const store = link.helper.logStore;
  store.open(INSTANCE, '000001', {kind: 'startup_step', stage: 'building_image'}, 'docker build');
  store.write(INSTANCE, '000001', encoder.encode('step 1/8: FROM\nstep 2/8: RUN'));
  return link;
}

describe('HostConnection.openLogs', () => {
  test('opens no channel until a lease asks, then one channel serves every lease', async () => {
    const link = seededLink();
    await using connection = connectionOver(link);
    await connection.ready();
    const control = link.helper.spawned;
    const first = connection.openLogs();
    const second = connection.openLogs();
    expect(link.helper.spawned).toBe(control);

    const listed = await first.list(INSTANCE, NEVER);
    expect(listed).toMatchObject({ok: true, instanceId: INSTANCE, streams: [{id: '000001'}]});
    const read = await second.read(
      {
        instance_id: INSTANCE,
        stream_id: '000001',
        start: {tail_bytes: 1024},
        max_bytes: 1024,
        wait_ms: 0,
      },
      NEVER,
    );
    expect(read).toMatchObject({ok: true, chunk: {data: 'step 1/8: FROM\nstep 2/8: RUN'}});
    expect(link.helper.spawned).toBe(control + 1);
    await first[Symbol.asyncDispose]();
    await second[Symbol.asyncDispose]();
  });

  test('releasing the last lease ends the logs channel and leaves control serving', async () => {
    const link = seededLink();
    await using connection = connectionOver(link);
    const lease = connection.openLogs();
    await lease.list(INSTANCE, NEVER);
    const logsChannel = link.helper.channels.at(-1);
    if (logsChannel === undefined) throw new Error('no logs channel was started');

    const other = connection.openLogs();
    await lease[Symbol.asyncDispose]();
    expect(logsChannel.process.running).toBe(true);
    await other[Symbol.asyncDispose]();
    await logsChannel.process.exit;
    expect(logsChannel.process.running).toBe(false);
    expect(await connection.request({type: 'ping'})).toMatchObject({type: 'ping'});
  });

  test('a helper without logs answers unsupported and opens no channel', async () => {
    const link = seededLink();
    link.helper.features.delete('logs');
    await using connection = connectionOver(link);
    await connection.ready();
    const spawned = link.helper.spawned;
    await using lease = connection.openLogs();

    const listed = await lease.list(INSTANCE, NEVER);
    expect(listed).toMatchObject({ok: false, failure: {code: 'feature_not_negotiated'}});
    expect(link.helper.spawned).toBe(spawned);
  });

  test('a helper error is an outcome, not a rejection', async () => {
    const link = seededLink();
    await using connection = connectionOver(link);
    await using lease = connection.openLogs();
    expect(await lease.list('fedcba987654', NEVER)).toMatchObject({
      ok: false,
      failure: {code: 'unknown_instance'},
    });
  });

  test('a request after the logs channel ended opens a new one', async () => {
    const link = seededLink();
    await using connection = connectionOver(link);
    await using lease = connection.openLogs();
    await lease.list(INSTANCE, NEVER);
    const ended = link.helper.channels.at(-1);
    ended?.end(null);
    await ended?.process.exit;
    const spawned = link.helper.spawned;

    expect(await lease.list(INSTANCE, NEVER)).toMatchObject({ok: true});
    expect(link.helper.spawned).toBe(spawned + 1);
  });

  test('an aborted request rejects with its reason', async () => {
    const link = seededLink();
    await using connection = connectionOver(link);
    await using lease = connection.openLogs();
    const abort = new AbortController();
    abort.abort(new Error('the view left'));
    await expect(lease.list(INSTANCE, abort.signal)).rejects.toThrow('the view left');
  });

  for (const offered of [true, false]) {
    test(`aborting a read in flight ${offered ? 'withdraws it' : 'only drops it'} when the helper ${offered ? 'offers' : 'lacks'} logs_cancel`, async () => {
      const link = seededLink();
      if (!offered) link.helper.features.delete('logs_cancel');
      await using connection = connectionOver(link);
      await using lease = connection.openLogs();
      const reads = 3;
      for (let index = 0; index < reads; index += 1) {
        const held = link.helper.deferNext('logs.read');
        const abort = new AbortController();
        const read = lease.read(WAITING_READ, abort.signal);
        await held.received;
        abort.abort(new Error('the view left'));
        await expect(read).rejects.toThrow('the view left');
        held.release();
      }
      // The channel keeps order: a withdrawal written before this request reached the helper first.
      expect(await lease.list(INSTANCE, NEVER)).toMatchObject({ok: true});

      const withdrawn = requestsOf(link, 'logs.cancel').map(cancel => cancel.read_id);
      const ids = requestsOf(link, 'logs.read').map(read => read.request_id);
      expect(ids).toHaveLength(reads);
      expect(withdrawn).toEqual(offered ? ids : []);
    });
  }

  test('a read that answered is not withdrawn by a later abort', async () => {
    const link = seededLink();
    await using connection = connectionOver(link);
    await using lease = connection.openLogs();
    const abort = new AbortController();
    expect(await lease.read(WAITING_READ, abort.signal)).toMatchObject({ok: true});
    abort.abort(new Error('the view left'));
    expect(await lease.list(INSTANCE, NEVER)).toMatchObject({ok: true});
    expect(requestsOf(link, 'logs.cancel')).toEqual([]);
  });

  test('stopping a following log follower withdraws its long poll', async () => {
    const link = seededLink();
    const clock = new FakeClock();
    await using connection = connectionOver(link, clock);
    await using lease = connection.openLogs();
    link.helper.deferNext('logs.read').release();
    const follow = link.helper.deferNext('logs.read');
    const follower = startLogFollower(lease, clock.scheduleTimeout, {
      instanceId: INSTANCE,
      streamId: '000001',
    });
    await follow.received;
    expect(follower.getSnapshot().value).toBe('following');

    follower.send({type: 'stop'});
    expect(await lease.list(INSTANCE, NEVER)).toMatchObject({ok: true});
    const [, waiting] = requestsOf(link, 'logs.read');
    if (waiting === undefined) throw new Error('the follower sent no follow read');
    expect(waiting.wait_ms).toBe(25_000);
    expect(requestsOf(link, 'logs.cancel').map(cancel => cancel.read_id)).toEqual([
      waiting.request_id,
    ]);
    follow.release();
  });
});
