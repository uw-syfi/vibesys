import {describe, it} from 'node:test';
import {
  ControlChannel,
  type ControlChannelState,
  type ControlConnection,
  type ControlConnectionHandlers,
  type IssuedRequest,
  type ScheduleTimeout,
} from './control-channel.js';
import {BackendClientError} from './errors.js';
import type {ProtocolResponse} from './protocol.js';
import {expect} from './test-support/expect.js';

/**
 * How one fake connection behaves once the channel holds it.
 *
 * - `answers`: a working connection; every request gets an `ok` response.
 * - `dropsWhileDialing`: reports a transport close through the handlers it was
 *   bound with, before `open()` returns it, then goes silent. This is what a
 *   socket that dies between `connect` and the channel installing it does: the
 *   close is reported once and never again.
 * - `faultsWhileDialing`: the same window, but with unreadable bytes, which is
 *   not an outage a redial recovers.
 * - `rejects`: the connector cannot establish a connection at all.
 */
type ConnectionBehavior = 'answers' | 'dropsWhileDialing' | 'faultsWhileDialing' | 'rejects';

/**
 * A `ControlConnector` with no transport under it: connections are plain
 * objects that answer, or fail, exactly as the script says. Requests are
 * answered synchronously from `send`, so a test awaits the request itself
 * rather than a delay, and dial behavior is driven by position in the script
 * rather than by timing.
 */
class FakeConnector {
  readonly opened: ConnectionBehavior[] = [];
  readonly sent: IssuedRequest[] = [];
  #script: ConnectionBehavior[];

  constructor(
    script: readonly ConnectionBehavior[],
    private readonly fallback: ConnectionBehavior = 'answers',
  ) {
    this.#script = [...script];
  }

  readonly open = async (handlers: ControlConnectionHandlers): Promise<ControlConnection> => {
    const behavior = this.#script.shift() ?? this.fallback;
    this.opened.push(behavior);
    if (behavior === 'rejects') {
      throw new BackendClientError('disconnected', 'dial refused');
    } else if (behavior === 'dropsWhileDialing') {
      handlers.onDrop(
        new BackendClientError('disconnected', 'socket closed before the dial returned'),
      );
    } else if (behavior === 'faultsWhileDialing') {
      // Typed the way a real transport types it: `streamFailure` classifies
      // bytes this protocol cannot read as `parse`.
      handlers.onFault(new BackendClientError('parse', 'unreadable banner'));
    }
    // `open` is a promise, so the channel always installs a connection at least
    // one microtask after the handlers are bound. That is the window.
    await Promise.resolve();
    return {
      send: frame => {
        const request = JSON.parse(frame) as IssuedRequest;
        this.sent.push(request);
        if (behavior !== 'answers') return;
        handlers.onFrame(
          JSON.stringify({
            protocol_version: 1,
            request_id: request.request_id,
            ok: true,
          } satisfies ProtocolResponse),
        );
      },
      close: async () => undefined,
    };
  };

  readonly runDedicated = async (): Promise<ProtocolResponse> => {
    throw new Error('no test here routes a request onto its own connection');
  };
}

class FakeScheduler {
  readonly #scheduled = new Map<() => void, number>();

  readonly schedule: ScheduleTimeout = (callback, delayMs) => {
    this.#scheduled.set(callback, delayMs);
    return () => this.#scheduled.delete(callback);
  };

  pendingDelays(): number[] {
    return [...this.#scheduled.values()];
  }
}

function deferred(): {readonly promise: Promise<void>; readonly resolve: () => void} {
  let settle = (): void => {};
  const promise = new Promise<void>(resolve => {
    settle = () => resolve();
  });
  return {promise, resolve: settle};
}

function channelWith(
  connector: FakeConnector,
  reconnectDelaysMs: readonly number[],
  trace: string[],
): ControlChannel {
  return new ControlChannel(connector, {
    clientId: 'test-client',
    reconnectDelaysMs,
    // The same rendering `node/client.test.ts` traces with: a report says more
    // than up-or-down, so collapsing it to two strings would let a wrong
    // `retrying` or `everConnected` pass unnoticed.
    onConnectionState: (state: ControlChannelState) =>
      trace.push(
        state.status === 'connected'
          ? 'connected'
          : `down:${state.everConnected ? 'lost' : 'cold'}${state.retrying ? ':retrying' : ''}`,
      ),
  });
}

describe('ControlChannel', () => {
  it('does not open a connection after a state callback closes the channel', async () => {
    const connector = new FakeConnector(['answers']);
    const adopted: {handlers?: ControlConnectionHandlers} = {};
    let closing: Promise<void> | null = null;
    let channel: ControlChannel;
    channel = new ControlChannel(connector, {
      clientId: 'test-client',
      reconnectDelaysMs: [],
      onConnectionState: state => {
        if (state.status !== 'disconnected') return;
        if (state.retrying) closing = channel.close();
        else channel.reconnect();
      },
    });
    channel.adopt(handlers => {
      adopted.handlers = handlers;
      return {send: () => undefined, close: async () => undefined};
    });

    if (adopted.handlers === undefined) throw new Error('The adopted connection has no handlers');
    adopted.handlers.onDrop(new BackendClientError('disconnected', 'socket closed'));
    if (closing === null) throw new Error('The retrying callback did not close the channel');
    await closing;

    expect(connector.opened).toEqual([]);
    expect(channel.connected).toBe(false);
  });

  for (const report of ['drop', 'fault'] as const) {
    it(`lets close own teardown from the initial ${report} callback`, async () => {
      const connector = new FakeConnector([]);
      const scheduler = new FakeScheduler();
      const connectionClosed = deferred();
      const adopted: {handlers?: ControlConnectionHandlers} = {};
      let connectionCloseCalls = 0;
      let closing: Promise<void> | null = null;
      let channel: ControlChannel;
      channel = new ControlChannel(connector, {
        clientId: 'test-client',
        reconnectDelaysMs: [10],
        scheduleTimeout: scheduler.schedule,
        onConnectionState: state => {
          if (state.status === 'disconnected' && !state.retrying) closing = channel.close();
        },
      });
      channel.adopt(handlers => {
        adopted.handlers = handlers;
        return {
          send: () => undefined,
          close: () => {
            connectionCloseCalls += 1;
            return connectionClosed.promise;
          },
        };
      });

      if (adopted.handlers === undefined) throw new Error('The adopted connection has no handlers');
      const error = new BackendClientError(
        report === 'drop' ? 'disconnected' : 'parse',
        report === 'drop' ? 'socket closed' : 'invalid frame',
      );
      if (report === 'drop') adopted.handlers.onDrop(error);
      else adopted.handlers.onFault(error);
      const closePromise = closing as Promise<void> | null;
      if (closePromise === null) throw new Error('The disconnected callback did not close');
      let closeSettled = false;
      void closePromise.then(() => {
        closeSettled = true;
      });
      await Promise.resolve();

      expect(connectionCloseCalls).toBe(1);
      expect(closeSettled).toBe(false);
      expect(scheduler.pendingDelays()).toEqual([]);

      connectionClosed.resolve();
      await closePromise;
      expect(closeSettled).toBe(true);
      expect(channel.connected).toBe(false);
    });
  }

  it('does not leave a retry timer when a failed-dial callback closes the channel', async () => {
    const connector = new FakeConnector(['rejects']);
    const scheduler = new FakeScheduler();
    const reported = deferred();
    let closing: Promise<void> | null = null;
    let channel: ControlChannel;
    channel = new ControlChannel(connector, {
      clientId: 'test-client',
      reconnectDelaysMs: [10],
      scheduleTimeout: scheduler.schedule,
      onConnectionState: state => {
        if (state.status !== 'disconnected' || state.retrying) return;
        closing = channel.close();
        reported.resolve();
      },
    });

    channel.reconnect();
    await reported.promise;
    const closePromise = closing as Promise<void> | null;
    if (closePromise === null) throw new Error('The failed-dial callback did not close');
    await closePromise;

    expect(connector.opened).toEqual(['rejects']);
    expect(scheduler.pendingDelays()).toEqual([]);
    expect(channel.connected).toBe(false);
  });

  it('does not install a connection that failed before the dial returned', async () => {
    // Two attempts: the first connection dies in the window between the
    // handlers being bound and `open()` resolving, the second works.
    const connector = new FakeConnector(['dropsWhileDialing', 'answers']);
    const trace: string[] = [];
    const channel = channelWith(connector, [0, 0], trace);

    channel.reconnect();
    // Installing the dead connection would make this request wait out its
    // response deadline: `connected` would be true, nothing would answer, and
    // no further close would ever be reported for a socket that already closed.
    const response = await channel.request({type: 'command.pause'});

    expect(response.ok).toBe(true);
    expect(connector.opened).toEqual(['dropsWhileDialing', 'answers']);
    // The dead connection is never written to, so the request was not delivered
    // into a socket that cannot answer it.
    expect(connector.sent).toHaveLength(1);
    expect(channel.connected).toBe(true);
    // The failed attempt, the retry going in, and the recovery. Never
    // `connected` for the dead connection.
    expect(trace).toEqual(['down:cold', 'down:cold:retrying', 'connected']);
    await channel.close();
  });

  it('fails a request when every dial loses its connection in that window', async () => {
    // The schedule is finite, so a channel that can never install a connection
    // has to answer the caller rather than hold the request forever.
    const connector = new FakeConnector([], 'dropsWhileDialing');
    const trace: string[] = [];
    const channel = channelWith(connector, [0, 0], trace);

    channel.reconnect();
    await expect(channel.request({type: 'command.pause'})).rejects.toMatchObject({
      kind: 'disconnected',
    });

    // One immediate attempt, then one per schedule entry: `reconnect()` dials
    // now rather than waiting out a delay before the first try.
    expect(connector.opened).toEqual([
      'dropsWhileDialing',
      'dropsWhileDialing',
      'dropsWhileDialing',
    ]);
    expect(connector.sent).toEqual([]);
    expect(channel.connected).toBe(false);
    // Each attempt is reported going in and coming out, and the channel never
    // claims to have connected.
    expect(trace).toEqual([
      'down:cold',
      'down:cold:retrying',
      'down:cold',
      'down:cold:retrying',
      'down:cold',
    ]);
    await channel.close();
  });

  it('takes a fault in that window as a fault rather than an outage', async () => {
    // A fault is not a transient outage, so the attempt does not spend the rest
    // of the schedule re-reading bytes this client cannot read.
    const connector = new FakeConnector(['faultsWhileDialing', 'answers']);
    const trace: string[] = [];
    const channel = channelWith(connector, [0, 0], trace);

    channel.reconnect();
    await expect(channel.request({type: 'command.pause'})).rejects.toMatchObject({
      kind: 'parse',
    });

    expect(connector.opened).toEqual(['faultsWhileDialing']);
    expect(connector.sent).toEqual([]);
    expect(channel.connected).toBe(false);
    expect(trace).toEqual(['down:cold']);

    // `reconnect()` is the caller's decision that the fault was one-off, and it
    // still works: the channel is down, not broken.
    channel.reconnect();
    const response = await channel.request({type: 'command.pause'});
    expect(response.ok).toBe(true);
    expect(channel.connected).toBe(true);
    await channel.close();
  });
});
