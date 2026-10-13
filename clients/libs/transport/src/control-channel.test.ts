import {describe, it} from 'node:test';
import type {ProtocolResponse} from '@vibesys/contracts';
import {BackendClientError} from '@vibesys/contracts';
import {
  type AbortSignalLike,
  ControlChannel,
  type ControlChannelState,
  type ControlConnection,
  type ControlConnectionHandlers,
  type IssuedRequest,
} from './index.js';
import {expect} from './test-support/expect.js';
import {FakeClock} from './testing/index.js';

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

/**
 * A structural signal that changes state while its listener is being
 * registered. `dispatch` models a synchronous abort notification; `silent`
 * models an implementation that exposes the new state without dispatching.
 */
class RegistrationAbortSignal implements AbortSignalLike {
  aborted = false;
  readonly reason = new Error('registration lost ownership');
  readonly #listeners = new Set<() => void>();

  constructor(private readonly behavior: 'dispatch' | 'none' | 'silent') {}

  get listenerCount(): number {
    return this.#listeners.size;
  }

  addEventListener(_type: 'abort', listener: () => void): void {
    this.#listeners.add(listener);
    if (this.behavior === 'none') return;
    this.aborted = true;
    if (this.behavior === 'dispatch') listener();
  }

  removeEventListener(_type: 'abort', listener: () => void): void {
    this.#listeners.delete(listener);
  }
}

/** A signal whose listener stays physically retained when removal fails. */
class ThrowingRemovalAbortSignal implements AbortSignalLike {
  aborted = false;
  readonly reason = new Error('request abandoned');
  readonly removalError = new Error('listener removal failed');
  readonly #listeners = new Set<() => void>();

  addEventListener(_type: 'abort', listener: () => void): void {
    this.#listeners.add(listener);
  }

  removeEventListener(_type: 'abort', _listener: () => void): void {
    throw this.removalError;
  }

  abort(): void {
    this.aborted = true;
    for (const listener of [...this.#listeners]) listener();
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
  for (const behavior of ['dispatch', 'silent'] as const) {
    it(`does not send when abort ${behavior === 'dispatch' ? 'dispatches' : 'changes state'} during listener registration`, async () => {
      const connector = new FakeConnector([]);
      const signal = new RegistrationAbortSignal(behavior);
      const sent: IssuedRequest[] = [];
      let handlers: ControlConnectionHandlers | undefined;
      const channel = new ControlChannel(connector, {
        clientId: 'test-client',
        reconnectDelaysMs: [],
      });
      channel.adopt(bound => {
        handlers = bound;
        return {
          send: frame => sent.push(JSON.parse(frame) as IssuedRequest),
          close: async () => undefined,
        };
      });

      const rejected = channel.request({type: 'command.steer', text: 'must not run'}, {signal});

      // Let the pre-fix implementation's wrongly transmitted request finish,
      // so the silent-notification case fails by value instead of hanging.
      const wronglyIssued = sent[0];
      if (wronglyIssued !== undefined) {
        handlers?.onFrame(
          JSON.stringify({
            protocol_version: 1,
            request_id: wronglyIssued.request_id,
            ok: true,
          }),
        );
      }
      const outcome = await rejected.then(
        response => response,
        error => error,
      );

      expect(outcome).toBe(signal.reason);
      expect(sent).toEqual([]);
      expect(signal.listenerCount).toBe(0);

      // A response for an unknown, abandoned request cannot settle the next
      // request or poison the channel's correlation state.
      handlers?.onFrame(JSON.stringify({protocol_version: 1, request_id: 'abandoned', ok: true}));
      const next = channel.request({type: 'command.pause'});
      const issued = sent[0];
      if (issued === undefined) throw new Error('The live request was not sent');
      handlers?.onFrame(
        JSON.stringify({protocol_version: 1, request_id: issued.request_id, ok: true}),
      );
      await expect(next).resolves.toMatchObject({request_id: issued.request_id, ok: true});
      await channel.close();
    });
  }

  it('does not send when its deadline scheduler settles synchronously', async () => {
    const connector = new FakeConnector([]);
    const sent: IssuedRequest[] = [];
    let handlers: ControlConnectionHandlers | undefined;
    let scheduled = 0;
    let cancelled = 0;
    const channel = new ControlChannel(connector, {
      clientId: 'test-client',
      reconnectDelaysMs: [],
      scheduleTimeout: callback => {
        scheduled += 1;
        if (scheduled === 1) callback();
        return () => {
          cancelled += 1;
        };
      },
    });
    channel.adopt(bound => {
      handlers = bound;
      return {
        send: frame => sent.push(JSON.parse(frame) as IssuedRequest),
        close: async () => undefined,
      };
    });

    await expect(
      channel.request({type: 'command.steer', text: 'must not run'}),
    ).rejects.toMatchObject({kind: 'timeout'});
    expect(sent).toEqual([]);
    expect(cancelled).toBe(1);

    const next = channel.request({type: 'command.pause'});
    const issued = sent[0];
    if (issued === undefined) throw new Error('The live request was not sent');
    handlers?.onFrame(
      JSON.stringify({protocol_version: 1, request_id: issued.request_id, ok: true}),
    );
    await expect(next).resolves.toMatchObject({request_id: issued.request_id, ok: true});
    await channel.close();
  });

  it('rolls back pending ownership when its deadline scheduler throws', async () => {
    const connector = new FakeConnector([]);
    const sent: IssuedRequest[] = [];
    const signal = new RegistrationAbortSignal('none');
    const schedulerError = new Error('scheduler failed');
    let handlers: ControlConnectionHandlers | undefined;
    let scheduled = 0;
    const channel = new ControlChannel(connector, {
      clientId: 'test-client',
      reconnectDelaysMs: [],
      scheduleTimeout: () => {
        scheduled += 1;
        if (scheduled === 1) throw schedulerError;
        return () => undefined;
      },
    });
    channel.adopt(bound => {
      handlers = bound;
      return {
        send: frame => sent.push(JSON.parse(frame) as IssuedRequest),
        close: async () => undefined,
      };
    });

    await expect(
      channel.request({type: 'command.steer', text: 'must not run'}, {signal}),
    ).rejects.toBe(schedulerError);
    expect(sent).toEqual([]);
    expect(signal.listenerCount).toBe(0);

    const next = channel.request({type: 'command.pause'});
    const issued = sent[0];
    if (issued === undefined) throw new Error('The live request was not sent');
    handlers?.onFrame(
      JSON.stringify({protocol_version: 1, request_id: issued.request_id, ok: true}),
    );
    await expect(next).resolves.toMatchObject({request_id: issued.request_id, ok: true});
    await channel.close();
  });

  it('settles abort and stays routable when physical listener removal throws', async () => {
    const connector = new FakeConnector([]);
    const signal = new ThrowingRemovalAbortSignal();
    const sent: IssuedRequest[] = [];
    let handlers: ControlConnectionHandlers | undefined;
    const channel = new ControlChannel(connector, {
      clientId: 'test-client',
      reconnectDelaysMs: [],
    });
    channel.adopt(bound => {
      handlers = bound;
      return {
        send: frame => sent.push(JSON.parse(frame) as IssuedRequest),
        close: async () => undefined,
      };
    });

    const abandoned = channel.request({type: 'query.snapshot'}, {signal});
    expect(() => signal.abort()).not.toThrow();
    await expect(abandoned).rejects.toBe(signal.reason);
    expect(() => signal.abort()).not.toThrow();

    const abandonedRequest = sent[0];
    if (abandonedRequest === undefined) throw new Error('The abandoned request was not sent');
    handlers?.onFrame(
      JSON.stringify({
        protocol_version: 1,
        request_id: abandonedRequest.request_id,
        ok: true,
      }),
    );
    const next = channel.request({type: 'command.pause'});
    const issued = sent[1];
    if (issued === undefined) throw new Error('The live request was not sent');
    handlers?.onFrame(
      JSON.stringify({protocol_version: 1, request_id: issued.request_id, ok: true}),
    );
    await expect(next).resolves.toMatchObject({request_id: issued.request_id, ok: true});
    await channel.close();
  });

  it('close joins a dial that resolves at the connection-ownership handoff', async () => {
    const openStarted = deferred();
    const releaseOpen = deferred();
    const closeStarted = deferred();
    const connectionCloseCalled = deferred();
    const connectionClosed = deferred();
    let connectionCloseCalls = 0;
    const connector = {
      open: async (_handlers: ControlConnectionHandlers): Promise<ControlConnection> => {
        openStarted.resolve();
        await releaseOpen.promise;
        return {
          send: () => undefined,
          close: () => {
            connectionCloseCalls += 1;
            connectionCloseCalled.resolve();
            return connectionClosed.promise;
          },
        };
      },
      runDedicated: async (): Promise<ProtocolResponse> => {
        throw new Error('This scenario opens only a control connection');
      },
    };
    const channel = new ControlChannel(connector, {
      clientId: 'test-client',
      reconnectDelaysMs: [],
    });

    channel.reconnect();
    await openStarted.promise;
    let closing: Promise<void> | undefined;
    // `open()` resumes first and returns the connection. This callback is next,
    // before the channel continuation that installs or retires the connection.
    void releaseOpen.promise.then(() => {
      closing = channel.close();
      closeStarted.resolve();
    });
    releaseOpen.resolve();
    await closeStarted.promise;
    if (closing === undefined) throw new Error('The release did not close the channel');
    await connectionCloseCalled.promise;

    let closeSettled = false;
    void closing.then(() => {
      closeSettled = true;
    });
    await Promise.resolve();

    expect(connectionCloseCalls).toBe(1);
    expect(closeSettled).toBe(false);

    connectionClosed.resolve();
    await closing;
    expect(closeSettled).toBe(true);
    expect(channel.connected).toBe(false);
  });

  it('close settles every owner when a connection observer rejects the dial', async () => {
    const closeStarted = deferred();
    const connectionCloseCalled = deferred();
    const releaseConnectionClose = deferred();
    const observerError = new Error('connection observer failed');
    const retirementError = new Error('connection retirement failed');
    let connectionCloseCalls = 0;
    const connector = {
      open: async (handlers: ControlConnectionHandlers): Promise<ControlConnection> => ({
        send: () => {
          handlers.onDrop(new BackendClientError('disconnected', 'socket dropped during send'));
        },
        close: async () => {
          connectionCloseCalls += 1;
          connectionCloseCalled.resolve();
          await releaseConnectionClose.promise;
          throw retirementError;
        },
      }),
      runDedicated: async (): Promise<ProtocolResponse> => {
        throw new Error('This scenario opens only a control connection');
      },
    };
    let closing: Promise<void> | undefined;
    let channel: ControlChannel;
    channel = new ControlChannel(connector, {
      clientId: 'test-client',
      reconnectDelaysMs: [],
      onConnectionState: state => {
        if (state.status === 'connected') return;
        closing = channel.close();
        closeStarted.resolve();
        throw observerError;
      },
    });

    const request = channel.request({type: 'command.pause'});
    // Observe the request immediately; the terminal assertions below still
    // inspect its typed result after the intentionally failing close paths.
    void request.catch(() => undefined);
    await closeStarted.promise;
    const firstClose = closing;
    if (firstClose === undefined) throw new Error('The observer did not close the channel');
    const repeatedClose = channel.close();
    await connectionCloseCalled.promise;

    let closeSettled = false;
    void firstClose.then(
      () => {
        closeSettled = true;
      },
      () => {
        closeSettled = true;
      },
    );
    // Drain the fixed promise chain through the rejected dial completion. The
    // gated retirement is still an owned resource, so neither outcome is legal.
    for (let turn = 0; turn < 5; turn += 1) await Promise.resolve();
    expect(connectionCloseCalls).toBe(1);
    expect(closeSettled).toBe(false);

    releaseConnectionClose.resolve();
    // Dial ownership precedes retirement ownership, so the observer failure is
    // stable even though the later connection retirement also rejects.
    await expect(firstClose).rejects.toBe(observerError);
    await expect(repeatedClose).rejects.toBe(observerError);
    await expect(channel.close()).rejects.toBe(observerError);
    await expect(request).rejects.toMatchObject({kind: 'disconnected'});
    expect(channel.connected).toBe(false);
  });

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
      const scheduler = new FakeClock();
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
    const scheduler = new FakeClock();
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
