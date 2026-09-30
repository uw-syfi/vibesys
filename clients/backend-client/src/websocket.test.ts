import {describe, expect, it} from 'bun:test';
import type {
  BackendClientError,
  ControlChannelState,
  ProtocolResponse,
  RequestInput,
} from './index.js';
import {REQUEST_POLICIES} from './index.js';
import {type WebSocketLike, WebSocketTransport} from './websocket.js';

const URL = 'ws://127.0.0.1:43123';

class FakeSocket implements WebSocketLike {
  readyState = 0;
  onopen: (() => void) | null = null;
  onmessage: ((event: {readonly data: unknown}) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: (() => void) | null = null;
  readonly sent: string[] = [];
  closeCalls = 0;
  /** Throw from `send` once this many frames have been accepted; null never. */
  failSendAfter: number | null = null;
  sendFailures = 0;
  #answered = 0;

  open(): void {
    this.readyState = 1;
    this.onopen?.();
  }

  send(data: string): void {
    if (this.failSendAfter !== null && this.sent.length >= this.failSendAfter) {
      // A real `WebSocket.send` throws on a socket the runtime has already
      // torn down, and it throws synchronously, which is what makes it able to
      // re-enter the channel's disposition path mid-flush.
      this.sendFailures += 1;
      throw new Error('WebSocket send failed');
    }
    this.sent.push(data);
  }

  respond(message: unknown): void {
    this.onmessage?.({data: JSON.stringify(message)});
  }

  receive(data: unknown): void {
    this.onmessage?.({data});
  }

  close(): void {
    this.closeCalls += 1;
    this.readyState = 3;
    this.onclose?.();
  }

  /** The peer went away: a close this socket's owner did not ask for. */
  drop(): void {
    this.readyState = 3;
    this.onclose?.();
  }

  /** The socket failed, as a browser reports a refused or broken connection. */
  fail(): void {
    this.onerror?.();
  }

  frames(): Array<Record<string, unknown>> {
    return this.sent.map(frame => JSON.parse(frame) as Record<string, unknown>);
  }

  requestIds(): string[] {
    return this.frames().map(frame => String(frame['request_id']));
  }

  /** Answer every frame this socket has received and not yet answered. */
  answerAll(body: Record<string, unknown> = {}): void {
    const frames = this.frames();
    while (this.#answered < frames.length) {
      const frame = frames[this.#answered];
      this.#answered += 1;
      if (frame !== undefined) this.respond(okResponse(String(frame['request_id']), body));
    }
  }
}

/**
 * A fake gateway that hands out sockets and remembers them, so a test can
 * inspect what each connection carried and drop any of them.
 */
class FakeGateway {
  readonly sockets: FakeSocket[] = [];
  /** How the next dial resolves. */
  outcome: 'open' | 'fail' | 'stall' = 'open';
  /** `FakeSocket.failSendAfter` for every socket dialed from now on. */
  failSendAfter: number | null = null;
  refusedDials = 0;

  readonly connect = (): WebSocketLike => {
    const socket = new FakeSocket();
    socket.failSendAfter = this.failSendAfter;
    this.sockets.push(socket);
    if (this.outcome === 'open') queueMicrotask(() => socket.open());
    if (this.outcome === 'fail') {
      this.refusedDials += 1;
      queueMicrotask(() => socket.fail());
    }
    return socket;
  };

  /** How many writes the gateway refused across every socket it handed out. */
  sendFailures(): number {
    return this.sockets.reduce((total, socket) => total + socket.sendFailures, 0);
  }

  /** Put the gateway back in working order, for a test's drain phase. */
  recover(): void {
    this.outcome = 'open';
    this.failSendAfter = null;
  }

  socket(index: number): FakeSocket {
    const socket = this.sockets[index];
    if (socket === undefined) throw new Error(`No socket at index ${index}`);
    return socket;
  }

  /** The control socket the transport is using now, if it has one. */
  live(): FakeSocket | undefined {
    return this.sockets.filter(socket => socket.readyState === 1).at(-1);
  }
}

class FakeScheduler {
  readonly #entries: Array<{callback: () => void; delayMs: number; cancelled: boolean}> = [];

  readonly schedule = (callback: () => void, delayMs = 0): (() => void) => {
    const entry = {callback, delayMs, cancelled: false};
    this.#entries.push(entry);
    return () => {
      entry.cancelled = true;
    };
  };

  runPending(): void {
    for (const entry of this.#entries.splice(0)) {
      if (!entry.cancelled) entry.callback();
    }
  }

  /**
   * Fire only the timers due within `maxDelayMs`, so a test can advance the
   * redial schedule without also expiring the request deadlines that are
   * minutes away.
   */
  runDue(maxDelayMs: number): void {
    const due = this.#entries.filter(entry => entry.delayMs <= maxDelayMs);
    for (const entry of due) {
      this.#entries.splice(this.#entries.indexOf(entry), 1);
      if (!entry.cancelled) entry.callback();
    }
  }
}

/**
 * One reported state as a short string, so a sequence of them reads as a
 * sequence. Names every field a frontend renders, which is also what makes the
 * report dedup checkable: an assertion on the whole trace fails if a transition
 * is emitted twice or swallowed.
 */
function trace(state: ControlChannelState): string {
  if (state.status === 'connected') return 'connected';
  return `down:${state.everConnected ? 'lost' : 'cold'}${state.retrying ? ':retrying' : ''}`;
}

const okResponse = (requestId: string, body: Record<string, unknown> = {}) => ({
  protocol_version: 1,
  request_id: requestId,
  ok: true,
  ...body,
});

const response = okResponse;

describe('WebSocketTransport', () => {
  it('keeps control messages one text frame and correlates the response', async () => {
    const sockets: FakeSocket[] = [];
    const transport = new WebSocketTransport(URL, {
      clientId: 'browser-client',
      webSocket: () => {
        const socket = new FakeSocket();
        sockets.push(socket);
        queueMicrotask(() => socket.open());
        return socket;
      },
    });

    const pending = transport.request({type: 'query.tui_defaults'});
    await tick();
    const frame = JSON.parse(sockets[0]?.sent[0] ?? '{}') as {
      request_id?: string;
      client_id?: string;
    };
    expect(sockets[0]?.sent[0]?.endsWith('\n')).toBe(false);
    expect(frame.client_id).toBe('browser-client');
    sockets[0]?.respond(response(frame.request_id ?? '', {tui_defaults: {theme: 'default'}}));
    await expect(pending).resolves.toMatchObject({ok: true});
    await transport.close();
  });

  it('delivers the subscribe acknowledgement and batch before resolving the handle', async () => {
    const sockets: FakeSocket[] = [];
    const transport = new WebSocketTransport(URL, {
      clientId: 'browser-subscriber',
      webSocket: () => {
        const socket = new FakeSocket();
        sockets.push(socket);
        queueMicrotask(() => socket.open());
        return socket;
      },
    });
    const messages: string[] = [];
    const subscription = transport.subscribe(
      0,
      message => messages.push(message.type ?? ''),
      error => {
        throw error;
      },
    );
    await tick();
    expect(JSON.parse(sockets[0]?.sent[0] ?? '{}')).toMatchObject({
      type: 'subscribe',
      client_id: 'browser-subscriber',
    });
    sockets[0]?.respond({
      type: 'subscribed',
      request_id: 'subscribe-1',
      run_id: 'run-1',
      latest_sequence: 2,
    });
    sockets[0]?.respond({type: 'event_batch', events: [], history_after_sequence: 0});
    await expect(subscription).resolves.toBeDefined();
    expect(messages).toEqual(['subscribed', 'event_batch']);
    await transport.close();
  });

  it('bounds the subscription handshake and closes an unresponsive socket', async () => {
    const scheduler = new FakeScheduler();
    const socket = new FakeSocket();
    const transport = new WebSocketTransport(URL, {
      connectTimeoutMs: 10,
      scheduleTimeout: scheduler.schedule,
      webSocket: () => {
        queueMicrotask(() => socket.open());
        return socket;
      },
    });

    const subscription = transport.subscribe(
      0,
      () => {},
      () => {},
    );
    await tick();
    scheduler.runPending();

    await expect(subscription).rejects.toMatchObject({kind: 'timeout'});
    expect(socket.closeCalls).toBe(1);
    await transport.close();
  });

  it('rejects a structured subscription refusal without waiting for close', async () => {
    const socket = new FakeSocket();
    const transport = transportFor(socket);
    const messages: string[] = [];
    const subscription = transport.subscribe(
      0,
      message => messages.push(message.type ?? ''),
      () => {},
    );
    await tick();

    socket.respond({type: 'protocol_error', code: 'stream_failed', message: 'not available'});

    await expect(subscription).rejects.toMatchObject({kind: 'rejected', message: 'not available'});
    expect(messages).toEqual(['protocol_error']);
    expect(socket.closeCalls).toBe(1);
    await transport.close();
  });

  it('closes a subscribed stream after a malformed frame', async () => {
    const socket = new FakeSocket();
    const transport = transportFor(socket);
    const disconnects: Error[] = [];
    const subscription = transport.subscribe(
      0,
      () => {},
      error => disconnects.push(error),
    );
    await tick();
    socket.respond({
      type: 'subscribed',
      request_id: 'subscribe-1',
      run_id: 'run-1',
      latest_sequence: 0,
    });
    await subscription;

    socket.receive('{');

    expect(disconnects).toHaveLength(1);
    expect(disconnects[0]).toMatchObject({kind: 'parse'});
    expect(socket.closeCalls).toBe(1);
    await transport.close();
  });

  it('closes a corrupted control socket before redialing', async () => {
    const sockets: FakeSocket[] = [];
    const transport = new WebSocketTransport(URL, {
      webSocket: () => {
        const socket = new FakeSocket();
        sockets.push(socket);
        queueMicrotask(() => socket.open());
        return socket;
      },
    });
    const first = transport.request({type: 'query.tui_defaults'});
    await tick();

    sockets[0]?.receive('{');
    await expect(first).rejects.toMatchObject({kind: 'parse'});
    expect(sockets[0]?.closeCalls).toBe(1);

    const second = transport.request({type: 'query.tui_defaults'});
    await tick();
    const frame = JSON.parse(sockets[1]?.sent[0] ?? '{}') as {request_id?: string};
    sockets[1]?.respond(response(frame.request_id ?? '', {tui_defaults: {theme: 'default'}}));
    await expect(second).resolves.toMatchObject({ok: true});
    await transport.close();
  });

  it('closes a socket still connecting and rejects its request', async () => {
    const socket = new FakeSocket();
    const transport = new WebSocketTransport(URL, {
      webSocket: () => socket,
    });
    const pending = transport.request({type: 'query.tui_defaults'});

    await transport.close();

    await expect(pending).rejects.toMatchObject({kind: 'disconnected'});
    expect(socket.closeCalls).toBe(1);
  });

  it('does not report a disconnect when client close owns subscription teardown', async () => {
    const socket = new FakeSocket();
    const transport = transportFor(socket);
    const disconnects: Error[] = [];
    const subscription = transport.subscribe(
      0,
      () => {},
      error => disconnects.push(error),
    );
    await tick();
    socket.respond({
      type: 'subscribed',
      request_id: 'subscribe-1',
      run_id: 'run-1',
      latest_sequence: 0,
    });
    await subscription;

    await transport.close();

    expect(disconnects).toEqual([]);
    expect(socket.closeCalls).toBe(1);
  });

  it('redials the control channel on the backoff schedule and carries a later command', async () => {
    const gateway = new FakeGateway();
    const scheduler = new FakeScheduler();
    const states: ControlChannelState[] = [];
    const transport = controlTransport(gateway, scheduler, {
      reconnectDelaysMs: [0],
      onConnectionState: state => states.push(state),
    });

    const first = transport.request({type: 'query.snapshot'});
    await tick();
    gateway.socket(0).answerAll();
    await expect(first).resolves.toMatchObject({ok: true});
    expect(transport.connected).toBe(true);

    gateway.socket(0).drop();
    expect(transport.connected).toBe(false);

    // The schedule redials on its own: no further request triggers it.
    scheduler.runDue(0);
    await tick();
    expect(gateway.sockets).toHaveLength(2);
    expect(transport.connected).toBe(true);
    // The redial's own start is reported too, so an affordance bound to
    // `retrying` knows a click would be a no-op while it is in flight.
    expect(states.map(trace)).toEqual([
      'connected',
      'down:lost',
      'down:lost:retrying',
      'connected',
    ]);

    const second = transport.request({type: 'command.pause'});
    await tick();
    gateway.socket(1).answerAll();
    await expect(second).resolves.toMatchObject({ok: true});
    await transport.close();
  });

  it('holds an idempotent request across a drop and resends it with a fresh id', async () => {
    const gateway = new FakeGateway();
    const scheduler = new FakeScheduler();
    const transport = controlTransport(gateway, scheduler, {reconnectDelaysMs: [0]});

    const pending = transport.request({type: 'command.pause'});
    await tick();
    const abandonedId = gateway.socket(0).requestIds()[0] ?? '';

    gateway.socket(0).drop();
    scheduler.runDue(0);
    await tick();

    const resent = gateway.socket(1).frames()[0] ?? {};
    expect(resent['type']).toBe('command.pause');
    expect(resent['request_id']).not.toBe(abandonedId);
    // The destroyed socket's late answer must resolve nothing.
    gateway.socket(0).respond(okResponse(abandonedId));
    gateway.socket(1).answerAll();
    await expect(pending).resolves.toMatchObject({
      ok: true,
      request_id: String(resent['request_id']),
    });
    await transport.close();
  });

  it('fails a non-idempotent request in flight at a drop and never resends it', async () => {
    const gateway = new FakeGateway();
    const scheduler = new FakeScheduler();
    const transport = controlTransport(gateway, scheduler, {reconnectDelaysMs: [0]});

    const pending = transport.request({type: 'command.steer', text: 'take the other branch'});
    await tick();
    expect(gateway.socket(0).frames()).toHaveLength(1);

    gateway.socket(0).drop();
    await expect(pending).rejects.toMatchObject({kind: 'disconnected', retryable: true});

    scheduler.runDue(0);
    await tick();
    // The channel recovered, and the steer reached only the first connection.
    expect(gateway.sockets).toHaveLength(2);
    expect(transport.connected).toBe(true);
    expect(gateway.socket(1).frames()).toEqual([]);
    await transport.close();
  });

  it('resolves an in-flight request whose response arrives before the drop', async () => {
    const gateway = new FakeGateway();
    const scheduler = new FakeScheduler();
    const transport = controlTransport(gateway, scheduler, {reconnectDelaysMs: []});

    const pending = transport.request({type: 'query.snapshot'});
    await tick();
    gateway.socket(0).answerAll();
    gateway.socket(0).drop();

    await expect(pending).resolves.toMatchObject({ok: true});
    await transport.close();
  });

  it('reports the control channel dead once its schedule is spent, and revives on a request', async () => {
    const gateway = new FakeGateway();
    const scheduler = new FakeScheduler();
    const states: ControlChannelState[] = [];
    const transport = controlTransport(gateway, scheduler, {
      reconnectDelaysMs: [],
      onConnectionState: state => states.push(state),
    });

    const pending = transport.request({type: 'query.snapshot'});
    await tick();
    gateway.socket(0).drop();

    // No schedule to redial on: the outage exhausts at once, the held request
    // fails typed rather than hanging, and the dead channel is reportable.
    await expect(pending).rejects.toMatchObject({kind: 'disconnected', retryable: true});
    expect(transport.connected).toBe(false);
    // A spent schedule arms no dial, so the outage is reported as idle.
    expect(states.map(trace)).toEqual(['connected', 'down:lost']);

    const revived = transport.request({type: 'query.snapshot'});
    await tick();
    expect(gateway.sockets).toHaveLength(2);
    gateway.socket(1).answerAll();
    await expect(revived).resolves.toMatchObject({ok: true});
    expect(transport.connected).toBe(true);
    expect(states.map(trace)).toEqual([
      'connected',
      'down:lost',
      'down:lost:retrying',
      'connected',
    ]);
    await transport.close();
  });

  it('costs one bounded dial series while the gateway is down, not one dial per request', async () => {
    const gateway = new FakeGateway();
    gateway.outcome = 'fail';
    const scheduler = new FakeScheduler();
    const states: ControlChannelState[] = [];
    const transport = controlTransport(gateway, scheduler, {
      reconnectDelaysMs: [0],
      onConnectionState: state => states.push(state),
    });

    const snapshot = rejection(transport.request({type: 'query.snapshot'}));
    const pause = rejection(transport.request({type: 'command.pause'}));
    const steer = rejection(transport.request({type: 'command.steer', text: 'x'}));
    await tick();

    // One dial serves all three requests rather than each paying its own.
    expect(gateway.sockets).toHaveLength(1);
    expect(states.map(state => state.status)).toEqual(['disconnected']);
    // A request that must not be repeated does not wait out an outage.
    expect((await steer).kind).toBe('disconnected');

    scheduler.runDue(0);
    await tick();

    // The schedule is one attempt long, so the second failure ends the series.
    expect(gateway.sockets).toHaveLength(2);
    expect((await snapshot).kind).toBe('disconnected');
    expect((await pause).kind).toBe('disconnected');
    expect(transport.connected).toBe(false);
    await transport.close();
  });

  it('routes a request onto a dedicated connection when the options ask for it', async () => {
    const gateway = new FakeGateway();
    const scheduler = new FakeScheduler();
    const transport = controlTransport(gateway, scheduler, {requestTimeoutMs: 20});

    const pending = transport.request({type: 'query.snapshot'}, {dedicatedConnection: true});
    await tick();
    expect(gateway.sockets).toHaveLength(1);
    // A dedicated request carries no response deadline, so expiring every
    // timer the transport armed must not fail it.
    scheduler.runPending();
    gateway.socket(0).answerAll();

    await expect(pending).resolves.toMatchObject({ok: true});
    expect(gateway.socket(0).closeCalls).toBe(1);
    await transport.close();
  });

  it('runs a chat on its own connection because the policy table says so', async () => {
    const gateway = new FakeGateway();
    const scheduler = new FakeScheduler();
    const transport = controlTransport(gateway, scheduler, {});

    expect(REQUEST_POLICIES['query.chat']).toMatchObject({dedicatedConnection: true});
    const chat = transport.request({type: 'query.chat', text: 'what happened?'});
    const snapshot = transport.request({type: 'query.snapshot'});
    await tick();

    expect(gateway.sockets).toHaveLength(2);
    expect(gateway.socket(0).frames()[0]?.['type']).toBe('query.chat');
    expect(gateway.socket(1).frames()[0]?.['type']).toBe('query.snapshot');
    gateway.socket(0).answerAll({chat: {question: 'q', answer: 'a', effect: 'none'}});
    gateway.socket(1).answerAll();

    await expect(chat).resolves.toMatchObject({ok: true});
    await expect(snapshot).resolves.toMatchObject({ok: true});
    await transport.close();
  });

  it('honors a per-call timeout override', async () => {
    const gateway = new FakeGateway();
    const scheduler = new FakeScheduler();
    const transport = controlTransport(gateway, scheduler, {requestTimeoutMs: 30_000});

    const pending = transport.request({type: 'query.snapshot'}, {timeoutMs: 20});
    await tick();
    scheduler.runDue(20);
    await tick();
    // The override fires first, then everything else. A build that ignored the
    // override used to leave nothing due at 20ms and this test waited on a
    // promise that could not settle until the runner killed it; firing the 30s
    // default too makes that failure an assertion on the message instead.
    scheduler.runPending();

    await expect(pending).rejects.toMatchObject({
      kind: 'timeout',
      message: 'Server request timed out after 20ms',
    });
    await transport.close();
  });

  it('discards the late response of an aborted request and stays routable', async () => {
    const gateway = new FakeGateway();
    const scheduler = new FakeScheduler();
    const transport = controlTransport(gateway, scheduler, {});
    const controller = new AbortController();

    const aborted = rejection(
      transport.request({type: 'query.snapshot'}, {signal: controller.signal}),
    );
    await tick();
    const abandonedId = gateway.socket(0).requestIds()[0] ?? '';
    controller.abort();
    expect((await aborted).name).toBe('AbortError');

    // The abandoned request's late response must resolve nothing.
    gateway.socket(0).respond(okResponse(abandonedId));
    const next = transport.request({type: 'command.pause'});
    await tick();
    const nextId = gateway.socket(0).requestIds()[1] ?? '';
    expect(nextId).not.toBe(abandonedId);
    gateway.socket(0).respond(okResponse(nextId));

    await expect(next).resolves.toMatchObject({ok: true, request_id: nextId});
    await transport.close();
  });

  it('aborts an in-flight dedicated request with the abort reason', async () => {
    const gateway = new FakeGateway();
    const scheduler = new FakeScheduler();
    const transport = controlTransport(gateway, scheduler, {});
    const controller = new AbortController();

    const chat = rejection(
      transport.request({type: 'query.chat', text: 'hang'}, {signal: controller.signal}),
    );
    await tick();
    expect(gateway.sockets).toHaveLength(1);
    controller.abort();

    expect((await chat).name).toBe('AbortError');
    expect(gateway.socket(0).closeCalls).toBe(1);
    await transport.close();
  });

  it('rejects a request whose signal was already aborted without opening a socket', async () => {
    const gateway = new FakeGateway();
    const scheduler = new FakeScheduler();
    const transport = controlTransport(gateway, scheduler, {});
    const controller = new AbortController();
    controller.abort();

    const rejected = rejection(
      transport.request({type: 'query.snapshot'}, {signal: controller.signal}),
    );

    expect((await rejected).name).toBe('AbortError');
    expect(gateway.sockets).toHaveLength(0);
    await transport.close();
  });

  it('holds a first request of any type for its first dial, then refuses one after a failure', async () => {
    const gateway = new FakeGateway();
    const scheduler = new FakeScheduler();
    const states: ControlChannelState[] = [];
    const transport = controlTransport(gateway, scheduler, {
      reconnectDelaysMs: [0],
      onConnectionState: state => states.push(state),
    });

    // Cold start: no connection has ever worked or failed, so nothing has told
    // the caller the command path is dead. A steer therefore waits for the
    // first dial rather than being refused by a rule about outages.
    const steer = transport.request({type: 'command.steer', text: 'x'});
    await tick();
    expect(gateway.socket(0).frames()[0]?.['type']).toBe('command.steer');
    gateway.socket(0).answerAll();
    await expect(steer).resolves.toMatchObject({ok: true});

    // Outage: the channel has now reported that it cannot deliver, so the same
    // type is refused instead of queued, and the caller decides what to do.
    gateway.socket(0).drop();
    expect(states.map(state => state.status)).toEqual(['connected', 'disconnected']);
    const refused = rejection(transport.request({type: 'command.steer', text: 'x'}));
    expect((await refused).kind).toBe('disconnected');
    expect(gateway.socket(0).frames()).toHaveLength(1);
    await transport.close();
  });

  it('reconnect() dials immediately instead of waiting out an armed redial', async () => {
    const gateway = new FakeGateway();
    const scheduler = new FakeScheduler();
    const transport = controlTransport(gateway, scheduler, {reconnectDelaysMs: [60_000]});

    const first = transport.request({type: 'query.snapshot'});
    await tick();
    gateway.socket(0).answerAll();
    await expect(first).resolves.toMatchObject({ok: true});

    // The drop arms a redial a minute out, so a reconnect control has to
    // shorten that rather than queue behind it and do nothing observable.
    gateway.socket(0).drop();
    expect(transport.connected).toBe(false);
    transport.reconnect();
    await tick();
    expect(gateway.sockets).toHaveLength(2);
    expect(transport.connected).toBe(true);

    // The redial it replaced must not also fire and strand a second connection.
    scheduler.runPending();
    await tick();
    expect(gateway.sockets).toHaveLength(2);
    await transport.close();
  });

  it('reports a dial in flight, and a second reconnect during it opens nothing', async () => {
    const gateway = new FakeGateway();
    const scheduler = new FakeScheduler();
    const states: ControlChannelState[] = [];
    const transport = controlTransport(gateway, scheduler, {
      reconnectDelaysMs: [60_000],
      onConnectionState: state => states.push(state),
    });

    const first = transport.request({type: 'query.snapshot'});
    await tick();
    gateway.socket(0).answerAll();
    await expect(first).resolves.toMatchObject({ok: true});

    // The peer goes away and the next dial hangs in its handshake. That window
    // is exactly where `reconnect()` is inert, so the reported state has to say
    // a dial is already running or the affordance cannot know.
    gateway.outcome = 'stall';
    gateway.socket(0).drop();
    transport.reconnect();
    await tick();
    expect(gateway.sockets).toHaveLength(2);
    expect(states.map(trace)).toEqual(['connected', 'down:lost', 'down:lost:retrying']);

    // A second press while the handshake is outstanding must not stack another
    // socket onto it, and must not re-report the state it did not change.
    transport.reconnect();
    await tick();
    expect(gateway.sockets).toHaveLength(2);
    expect(states.map(trace)).toEqual(['connected', 'down:lost', 'down:lost:retrying']);

    // When the stalled handshake expires, the outage is reported again with no
    // dial in flight, so the affordance comes back to life.
    scheduler.runDue(5_000);
    await tick();
    expect(states.map(trace)).toEqual([
      'connected',
      'down:lost',
      'down:lost:retrying',
      'down:lost',
    ]);
    await transport.close();
  });

  it('reconnect() is inert on a live channel and after close, which cancels the redial', async () => {
    const gateway = new FakeGateway();
    const scheduler = new FakeScheduler();
    const transport = controlTransport(gateway, scheduler, {reconnectDelaysMs: [60_000]});

    const first = transport.request({type: 'query.snapshot'});
    await tick();
    gateway.socket(0).answerAll();
    await expect(first).resolves.toMatchObject({ok: true});

    // Nothing to reconnect: a press on a live channel must not replace the
    // connection the requests riding it are correlated against.
    transport.reconnect();
    await tick();
    expect(gateway.sockets).toHaveLength(1);

    gateway.socket(0).drop();
    await transport.close();
    // The redial the drop armed is cancelled, so a client that is gone does not
    // dial the gateway a minute after its owner stopped caring.
    scheduler.runPending();
    await tick();
    expect(gateway.sockets).toHaveLength(1);
    // And the affordance cannot resurrect a closed client either.
    transport.reconnect();
    await tick();
    expect(gateway.sockets).toHaveLength(1);
  });

  it('ignores a retired connection still reporting after its replacement is live', async () => {
    const gateway = new FakeGateway();
    const scheduler = new FakeScheduler();
    const states: ControlChannelState[] = [];
    const transport = controlTransport(gateway, scheduler, {
      reconnectDelaysMs: [0],
      onConnectionState: state => states.push(state),
    });

    const first = transport.request({type: 'query.snapshot'});
    await tick();
    gateway.socket(0).answerAll();
    await expect(first).resolves.toMatchObject({ok: true});

    gateway.socket(0).drop();
    scheduler.runDue(0);
    await tick();
    expect(gateway.sockets).toHaveLength(2);
    expect(transport.connected).toBe(true);

    // The old socket is still wired to the handlers its own dial bound, and a
    // real one can fire again while closing. Attributed to the generation that
    // opened it, so it cannot take the live connection down or be answered on.
    gateway.socket(0).drop();
    gateway.socket(0).receive(JSON.stringify(okResponse('never-issued')));
    await tick();
    expect(transport.connected).toBe(true);
    expect(states.map(trace)).toEqual([
      'connected',
      'down:lost',
      'down:lost:retrying',
      'connected',
    ]);

    const second = transport.request({type: 'query.snapshot'});
    await tick();
    gateway.socket(1).answerAll();
    await expect(second).resolves.toMatchObject({ok: true});
    await transport.close();
  });

  it('never resends a held request the caller abandoned during the outage', async () => {
    const gateway = new FakeGateway();
    const scheduler = new FakeScheduler();
    const transport = controlTransport(gateway, scheduler, {reconnectDelaysMs: [60_000]});

    const first = transport.request({type: 'query.snapshot'});
    await tick();
    gateway.socket(0).answerAll();
    await expect(first).resolves.toMatchObject({ok: true});

    // Two idempotent requests are held for the recovery; the caller gives up on
    // one of them while it is still held rather than in flight.
    const controller = new AbortController();
    const abandoned = rejection(
      transport.request({type: 'query.snapshot'}, {signal: controller.signal}),
    );
    const kept = transport.request({type: 'query.tui_defaults'});
    gateway.socket(0).drop();
    await tick();
    controller.abort();
    expect((await abandoned).name).toBe('AbortError');

    transport.reconnect();
    await tick();
    // Only the kept request rides the recovery. A held entry removed from the
    // queue must not be written to the fresh connection, or the caller gets a
    // command they explicitly abandoned.
    expect(gateway.socket(1).frames().map(frame => frame['type'])).toEqual(['query.tui_defaults']);
    gateway.socket(1).answerAll();
    await expect(kept).resolves.toMatchObject({ok: true});
    await transport.close();
  });

  it('fails the dial when the socket it was handed is already gone', async () => {
    const scheduler = new FakeScheduler();
    const sockets: FakeSocket[] = [];
    const transport = new WebSocketTransport(URL, {
      closeGraceMs: 0,
      scheduleTimeout: scheduler.schedule,
      reconnectDelaysMs: [],
      // An embedded runtime may hand back a socket that is already open, which
      // skips the handshake and so installs none of its handlers. If it dies in
      // the microtask before the caller adopts it, no listener hears the close
      // and `readyState` is the only evidence left.
      webSocket: () => {
        const socket = new FakeSocket();
        socket.readyState = 1;
        sockets.push(socket);
        queueMicrotask(() => socket.drop());
        return socket;
      },
    });

    const rejected = rejection(transport.request({type: 'query.snapshot'}));
    await tick();
    // Fire whatever is armed, so a build that hands the dead socket to the
    // channel fails on the request deadline instead of hanging this test.
    scheduler.runPending();

    expect(sockets).toHaveLength(1);
    // A dead socket is a failed dial, not a request that sits on a connection
    // nothing will ever answer.
    expect(await rejected).toMatchObject({kind: 'disconnected'});
    expect(sockets[0]?.sent).toEqual([]);
    await transport.close();
  });

  it('closes a control socket that close() raced before the dial was claimed', async () => {
    const scheduler = new FakeScheduler();
    const socket = new FakeSocket();
    const transport = new WebSocketTransport(URL, {
      closeGraceMs: 0,
      scheduleTimeout: scheduler.schedule,
      webSocket: () => socket,
    });

    const rejected = rejection(transport.request({type: 'query.snapshot'}));
    // Open the socket and close the client in one turn, so `close()` runs in
    // the window between the dial resolving and the channel adopting it.
    socket.open();
    const closing = transport.close();

    // Asserted before yielding: the socket has to be torn down by `close()`
    // itself, not by the dial's own continuation running afterwards, or
    // `close()` can return with a live socket behind it.
    expect(socket.closeCalls).toBe(1);
    await closing;
    expect(socket.readyState).toBe(3);
    expect(await rejected).toMatchObject({kind: 'disconnected'});
  });

  it('disposes the rest of a resend batch when one send fails mid-flush', async () => {
    const gateway = new FakeGateway();
    const scheduler = new FakeScheduler();
    const transport = controlTransport(gateway, scheduler, {reconnectDelaysMs: [0, 0]});

    const first = transport.request({type: 'command.pause'});
    const second = transport.request({type: 'query.snapshot'});
    const third = transport.request({type: 'query.tui_defaults'});
    await tick();
    expect(gateway.socket(0).frames()).toHaveLength(3);

    // The redial comes up but its socket refuses the second write, which
    // reports a drop while the flush is still walking the queue.
    gateway.socket(0).drop();
    gateway.failSendAfter = 1;
    scheduler.runDue(0);
    await tick();
    expect(gateway.socket(1).frames()).toHaveLength(1);
    expect(gateway.sendFailures()).toBe(1);

    // The next redial gets a healthy socket and must carry all three: none may
    // be left written to the dead socket and owed an answer by nobody.
    gateway.recover();
    scheduler.runDue(0);
    await tick();
    expect(
      gateway
        .socket(2)
        .frames()
        .map(frame => frame['type'])
        .sort(),
    ).toEqual(['command.pause', 'query.snapshot', 'query.tui_defaults']);

    gateway.socket(2).answerAll();
    await expect(Promise.all([first, second, third])).resolves.toHaveLength(3);
    await transport.close();
  });

  it('answers exactly one of two concurrent identical requests', async () => {
    const gateway = new FakeGateway();
    const scheduler = new FakeScheduler();
    const transport = controlTransport(gateway, scheduler, {});

    const first = transport.request({type: 'query.snapshot'});
    const second = transport.request({type: 'query.snapshot'});
    await tick();
    const ids = gateway.socket(0).requestIds();
    expect(new Set(ids).size).toBe(2);

    // Two indistinguishable requests still get their own ids, and answering one
    // settles one: a response reaches the request that issued it and no other.
    const settled: string[] = [];
    void first.then(value => settled.push(`first:${value.request_id}`));
    void second.then(value => settled.push(`second:${value.request_id}`));
    gateway.socket(0).respond(okResponse(ids[0] ?? ''));
    await tick();
    expect(settled).toEqual([`first:${ids[0]}`]);

    gateway.socket(0).respond(okResponse(ids[1] ?? ''));
    await tick();
    expect(settled).toEqual([`first:${ids[0]}`, `second:${ids[1]}`]);
    await transport.close();
  });

  it('stamps its own request id over one the caller supplied', async () => {
    const gateway = new FakeGateway();
    const scheduler = new FakeScheduler();
    const transport = controlTransport(gateway, scheduler, {});

    // `RequestInput` omits `request_id`, so a typed caller cannot supply one;
    // the cast is what a JavaScript caller, or anything that widens its way
    // past the type, actually does. A supplied id must not be able to collide
    // with, or replay, the one the channel is correlating on.
    const pending = transport.request({
      type: 'query.snapshot',
      request_id: 'caller-chosen',
    } as unknown as RequestInput);
    await tick();
    const written = gateway.socket(0).requestIds()[0] ?? '';
    expect(written).not.toBe('caller-chosen');

    gateway.socket(0).respond(okResponse('caller-chosen'));
    gateway.socket(0).respond(okResponse(written));
    await expect(pending).resolves.toMatchObject({ok: true, request_id: written});
    await transport.close();
  });

  it('queues a request issued from the connected report behind the resends', async () => {
    const gateway = new FakeGateway();
    const scheduler = new FakeScheduler();
    let reported = false;
    const transport: WebSocketTransport = controlTransport(gateway, scheduler, {
      onConnectionState: state => {
        if (state.status !== 'connected' || reported) return;
        reported = true;
        void transport.request({type: 'query.tui_defaults'}).catch(() => undefined);
      },
    });

    const held = transport.request({type: 'command.pause'});
    await tick();

    // The held request was issued first, so it must reach the server first: the
    // recovery is reported once its resends are on the wire, not before.
    expect(
      gateway
        .socket(0)
        .frames()
        .map(frame => frame['type']),
    ).toEqual(['command.pause', 'query.tui_defaults']);
    gateway.socket(0).answerAll();
    await expect(held).resolves.toMatchObject({ok: true});
    await transport.close();
  });

  // The table is the source of the resend rule, so the transport must follow it
  // for every type in it rather than for the two the examples above name.
  it('resends exactly the request types the policy table calls idempotent', async () => {
    for (const [type, policy] of Object.entries(REQUEST_POLICIES)) {
      if (policy.dedicatedConnection) continue;
      const gateway = new FakeGateway();
      const scheduler = new FakeScheduler();
      const transport = controlTransport(gateway, scheduler, {reconnectDelaysMs: [0]});
      const pending = transport.request(requestOf(type));
      const settled = policy.idempotent ? null : rejection(pending);
      await tick();

      gateway.socket(0).drop();
      scheduler.runDue(0);
      await tick();

      const resent = gateway.socket(1).frames().length;
      expect({type, resent: resent > 0}).toEqual({type, resent: policy.idempotent});
      if (settled !== null) expect((await settled).kind).toBe('disconnected');
      else {
        gateway.socket(1).answerAll();
        await expect(pending).resolves.toMatchObject({ok: true});
      }
      await transport.close();
    }
  });

  /**
   * For any interleaving of requests, answers, drops, refused dials, refused
   * writes, and redials: every request settles exactly once, no request id is
   * ever reused, a response reaches a request of the type that wrote its id and
   * reaches exactly one request, and a request type that must not be repeated
   * is written no more times than it was issued.
   */
  it('settles every request exactly once and never misroutes a response', async () => {
    const total = emptyStats();
    for (let seed = 1; seed <= 24; seed += 1) {
      const run = await runInterleaving(seed);
      for (const key of Object.keys(total) as Array<keyof InterleavingStats>) {
        total[key] += run[key];
      }
    }
    // The generator is only worth trusting if it reached the situations the
    // properties are about, so the counts are part of the assertion. Each floor
    // is about half of what these 24 seeds actually produce (191 issued, 120
    // resolved, 71 rejected, 20 resent, 34 drops, 38 refused dials, 4 refused
    // writes), which leaves room for the generator to shift without letting it
    // turn this test green by reaching nothing.
    expect(total.issued).toBeGreaterThan(90);
    expect(total.resolved).toBeGreaterThan(60);
    expect(total.rejected).toBeGreaterThan(35);
    expect(total.resent).toBeGreaterThan(10);
    expect(total.drops).toBeGreaterThan(16);
    expect(total.refusedDials).toBeGreaterThan(18);
    expect(total.sendFailures).toBeGreaterThan(2);
  });
});

const IDEMPOTENT_TYPES = [
  'query.snapshot',
  'query.tui_defaults',
  'command.pause',
  'command.resume',
] as const;
const NON_IDEMPOTENT_TYPES = ['command.steer', 'query.chat_thread_create'] as const;
const GENERATED_TYPES = [...IDEMPOTENT_TYPES, ...NON_IDEMPOTENT_TYPES];

interface TrackedRequest {
  readonly type: string;
  outcome: 'pending' | 'resolved' | 'rejected';
  settlements: number;
  responseId: string | null;
  error: Error | null;
}

interface InterleavingStats {
  issued: number;
  resolved: number;
  rejected: number;
  /** Request types written more often than they were issued: a real resend. */
  resent: number;
  /** Situations the generator has to reach for the properties to mean anything. */
  drops: number;
  refusedDials: number;
  sendFailures: number;
}

function emptyStats(): InterleavingStats {
  return {
    issued: 0,
    resolved: 0,
    rejected: 0,
    resent: 0,
    drops: 0,
    refusedDials: 0,
    sendFailures: 0,
  };
}

async function runInterleaving(seed: number): Promise<InterleavingStats> {
  const gateway = new FakeGateway();
  const scheduler = new FakeScheduler();
  const transport = controlTransport(gateway, scheduler, {reconnectDelaysMs: [0, 0]});
  const run = await driveInterleaving(seed, transport, gateway, scheduler);
  return checkInterleaving(seed, gateway, run);
}

interface Interleaving {
  readonly tracked: TrackedRequest[];
  readonly drops: number;
}

/**
 * Drive one pseudo-random interleaving of issue, answer, drop, refused dial,
 * refused write, and redial, then drain: put the gateway back in working order,
 * let the schedule recover, answer what is on the wire, and close, which fails
 * whatever the server never answered. Every loop is bounded, so an interleaving
 * cannot itself hang, and no step reads a clock or sleeps.
 */
async function driveInterleaving(
  seed: number,
  transport: WebSocketTransport,
  gateway: FakeGateway,
  scheduler: FakeScheduler,
): Promise<Interleaving> {
  const random = seededRandom(seed);
  const tracked: TrackedRequest[] = [];
  let drops = 0;
  for (let step = 0; step < 24; step += 1) {
    switch (random(6)) {
      case 0:
      case 1:
        issue(
          transport,
          GENERATED_TYPES[random(GENERATED_TYPES.length)] ?? 'query.snapshot',
          tracked,
        );
        break;
      case 2:
        gateway.live()?.answerAll();
        break;
      case 3:
        if (gateway.live() !== undefined) drops += 1;
        gateway.live()?.drop();
        break;
      case 4:
        scheduler.runDue(0);
        break;
      default:
        // Perturb the gateway. A dial that refuses and a socket that breaks on
        // a write are the paths a drop alone never reaches, and they are where
        // the schedule-spend and mid-flush disposition rules live.
        gateway.outcome = random(2) === 0 ? 'open' : 'fail';
        gateway.failSendAfter = random(3) === 0 ? 1 : null;
        break;
    }
    await tick(10);
  }
  gateway.recover();
  for (let pass = 0; pass < 6; pass += 1) {
    scheduler.runDue(0);
    await tick(10);
    gateway.live()?.answerAll();
    await tick(10);
  }
  await transport.close();
  await tick(10);
  return {tracked, drops};
}

/**
 * Assert the properties one finished interleaving must have, and count it.
 *
 * Correlation is checked per type rather than per request, because the test
 * cannot see which minted id belongs to which of two identical requests. That
 * is not a gap: two requests of the same type are indistinguishable to their
 * callers, so swapping their responses is unobservable, while a response
 * reaching a different type, or reaching two requests, is exactly what these
 * checks catch.
 */
function checkInterleaving(
  seed: number,
  gateway: FakeGateway,
  run: Interleaving,
): InterleavingStats {
  const writtenIds = gateway.sockets.flatMap(socket => socket.requestIds());
  expect(new Set(writtenIds).size).toBe(writtenIds.length);
  const stats = {
    ...emptyStats(),
    issued: run.tracked.length,
    drops: run.drops,
    refusedDials: gateway.refusedDials,
    sendFailures: gateway.sendFailures(),
  };
  const delivered = new Set<string>();
  for (const request of run.tracked) {
    if (request.outcome === 'resolved') stats.resolved += 1;
    if (request.outcome === 'rejected') stats.rejected += 1;
    checkRequest({seed, type: request.type}, request, idsFor(gateway, request.type), delivered);
  }
  for (const type of GENERATED_TYPES) {
    const issues = run.tracked.filter(request => request.type === type).length;
    const writes = idsFor(gateway, type).length;
    if (isIdempotent(type)) {
      if (writes > issues) stats.resent += 1;
      continue;
    }
    // A type that must not be repeated is written no more often than it was
    // issued, however the drops, refusals, and redials fell.
    const context = {seed, type};
    expect({...context, withinBudget: writes <= issues}).toEqual({...context, withinBudget: true});
  }
  return stats;
}

function checkRequest(
  context: {readonly seed: number; readonly type: string},
  request: TrackedRequest,
  writes: readonly string[],
  delivered: Set<string>,
): void {
  expect({...context, settlements: request.settlements}).toEqual({...context, settlements: 1});
  if (request.outcome !== 'resolved') {
    expect({...context, kind: (request.error as {kind?: string}).kind}).toEqual({
      ...context,
      kind: 'disconnected',
    });
    return;
  }
  const id = request.responseId ?? '';
  // The id it resolved with was written for its own type, and was delivered to
  // this request only: no response reaches two requests.
  expect({...context, own: writes.includes(id), reused: delivered.has(id)}).toEqual({
    ...context,
    own: true,
    reused: false,
  });
  delivered.add(id);
}

/** Issue one request and record how it settles. Duplicates of a type allowed. */
function issue(transport: WebSocketTransport, type: string, tracked: TrackedRequest[]): void {
  const record: TrackedRequest = {
    type,
    outcome: 'pending',
    settlements: 0,
    responseId: null,
    error: null,
  };
  tracked.push(record);
  void transport.request(requestOf(type)).then(
    (value: ProtocolResponse) => {
      record.settlements += 1;
      record.outcome = 'resolved';
      record.responseId = value.request_id;
    },
    (error: Error) => {
      record.settlements += 1;
      record.outcome = 'rejected';
      record.error = error;
    },
  );
}

/** Every request id the transport wrote for one request type, in order. */
function idsFor(gateway: FakeGateway, type: string): string[] {
  return gateway.sockets.flatMap(socket =>
    socket
      .frames()
      .filter(frame => frame['type'] === type)
      .map(frame => String(frame['request_id'])),
  );
}

function isIdempotent(type: string): boolean {
  return (IDEMPOTENT_TYPES as readonly string[]).includes(type);
}

function requestOf(type: string): RequestInput {
  if (type === 'command.steer') return {type, text: 'steer'};
  if (type === 'query.chat') return {type, text: 'chat'};
  return {type} as RequestInput;
}

/** A small deterministic generator, so a failing interleaving is reproducible. */
function seededRandom(seed: number): (bound: number) => number {
  let state = (seed * 2_654_435_761) % 4_294_967_291;
  return bound => {
    state = (state * 48_271) % 2_147_483_647;
    return state % bound;
  };
}

function controlTransport(
  gateway: FakeGateway,
  scheduler: FakeScheduler,
  options: {
    reconnectDelaysMs?: readonly number[];
    onConnectionState?: (state: ControlChannelState) => void;
    requestTimeoutMs?: number;
  },
): WebSocketTransport {
  return new WebSocketTransport(URL, {
    ...options,
    closeGraceMs: 0,
    webSocket: gateway.connect,
    scheduleTimeout: scheduler.schedule,
  });
}

function transportFor(socket: FakeSocket): WebSocketTransport {
  return new WebSocketTransport(URL, {
    webSocket: () => {
      queueMicrotask(() => socket.open());
      return socket;
    },
  });
}

/**
 * Capture a rejection as a value, so a test can await it after driving the
 * transport further without the pending promise becoming an unhandled one.
 */
function rejection(pending: Promise<unknown>): Promise<BackendClientError> {
  return pending.then(
    () => {
      throw new Error('Expected the request to reject');
    },
    (error: BackendClientError) => error,
  );
}

async function tick(turns = 5): Promise<void> {
  for (let turn = 0; turn < turns; turn += 1) await Promise.resolve();
}
