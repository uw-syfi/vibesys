import {describe, it} from 'node:test';
import {
  ControlChannel,
  type ControlChannelState,
  type ControlConnection,
  type ControlConnectionHandlers,
  type IssuedRequest,
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
 */
type ConnectionBehavior = 'answers' | 'dropsWhileDialing' | 'faultsWhileDialing';

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
    if (behavior === 'dropsWhileDialing') {
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
