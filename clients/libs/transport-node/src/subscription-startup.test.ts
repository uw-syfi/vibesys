import {describe, it} from 'node:test';
import {BackendClientError} from '@vibesys/contracts';
import type {ScheduleTimeout} from '@vibesys/transport';
import {type ClientClock, ServerClient} from './index.js';
import {expect} from './test-support/expect.js';
import {type ClientSocketListenerKind, FakeClientNetwork} from './testing/index.js';

const OPERATION_LISTENER_KINDS = [
  'connect',
  'data',
  'error',
  'peerEnd',
] as const satisfies readonly ClientSocketListenerKind[];
const CONTROL_LISTENER_FAILURES = [
  ['data', 1],
  // The dial owns the first error listener; the live connection asks for the second.
  ['error', 2],
  ['peerEnd', 1],
] as const satisfies ReadonlyArray<readonly [ClientSocketListenerKind, number]>;

class StartupClock implements ClientClock {
  readonly schedulerError = new Error('scheduler refused the subscription deadline');
  mode: 'hold' | 'immediate' | 'throw' = 'hold';
  cancellations = 0;

  now(): number {
    return 0;
  }

  readonly scheduleTimeout: ScheduleTimeout = callback => {
    if (this.mode === 'throw') throw this.schedulerError;
    if (this.mode === 'immediate') callback();
    let active = true;
    return () => {
      if (!active) return;
      active = false;
      this.cancellations += 1;
    };
  };
}

class ReentrantCloseClock implements ClientClock {
  calls = 0;
  client: ServerClient | null = null;
  closeResult: Promise<void> | null = null;

  now(): number {
    return 0;
  }

  readonly scheduleTimeout: ScheduleTimeout = callback => {
    this.calls += 1;
    // The first schedule is the initial dial. Deliver the redial delay during
    // registration, then close from inside the new dial's deadline scheduler.
    if (this.calls === 2) callback();
    if (this.calls === 3) {
      if (this.client === null) throw new Error('client missing during reentrant close');
      this.closeResult = this.client.close();
    }
    return () => undefined;
  };
}

async function connectedClient(network: FakeClientNetwork, clock: StartupClock) {
  const connecting = ServerClient.connect('/run/server.sock', {
    clock,
    connectTimeoutMs: 40,
    network,
  });
  expect(network.connections).toHaveLength(1);
  network.connections[0]?.connect();
  return await connecting;
}

function answerDedicatedRequest(network: FakeClientNetwork): void {
  const socket = network.connections[1];
  if (socket === undefined) throw new Error('dedicated request did not open a socket');
  socket.connect();
  const frame = socket.writes[0];
  if (frame === undefined) throw new Error('dedicated request did not write a frame');
  const request = JSON.parse(frame) as {request_id: string};
  socket.receive(
    `${JSON.stringify({
      protocol_version: 1,
      request_id: request.request_id,
      timestamp: '2026-01-01T00:00:00Z',
      ok: true,
      events: [],
    })}\n`,
  );
}

describe('ServerClient subscription startup ownership', () => {
  it('retires an initial dial when its deadline scheduler throws', async () => {
    const network = new FakeClientNetwork();
    const clock = new StartupClock();
    clock.mode = 'throw';

    const connecting = ServerClient.connect('/run/server.sock', {
      clock,
      connectTimeoutMs: 40,
      network,
    });

    await expect(connecting).rejects.toBe(clock.schedulerError);
    expect(network.connections).toHaveLength(1);
    expect(network.connections[0]?.closed).toBe(true);
    expect(network.liveConnections).toEqual([]);
  });

  it('adopts an immediately delivered dial deadline before rejecting', async () => {
    const network = new FakeClientNetwork();
    const clock = new StartupClock();
    clock.mode = 'immediate';

    const connecting = ServerClient.connect('/run/server.sock', {
      clock,
      connectTimeoutMs: 40,
      network,
    });

    await expect(connecting).rejects.toMatchObject({
      kind: 'timeout',
      message: 'Timed out connecting to server after 40ms',
    });
    expect(network.connections[0]?.closed).toBe(true);
    expect(network.liveConnections).toEqual([]);
    expect(clock.cancellations).toBe(1);
  });

  it('retires an initial dial when connect-listener acquisition throws', async () => {
    const network = new FakeClientNetwork();
    const listenerError = new Error('could not observe socket connection');
    network.failNextConnectListener(listenerError);
    const clock = new StartupClock();

    const connecting = ServerClient.connect('/run/server.sock', {
      clock,
      connectTimeoutMs: 40,
      network,
    });

    await expect(connecting).rejects.toBe(listenerError);
    expect(network.connections[0]?.closed).toBe(true);
    expect(network.liveConnections).toEqual([]);
    expect(clock.cancellations).toBe(1);
  });

  it('retires an initial dial when error-listener acquisition throws', async () => {
    const network = new FakeClientNetwork();
    const listenerError = new Error('could not observe socket errors');
    network.failNextErrorListener(listenerError);
    const clock = new StartupClock();

    const connecting = ServerClient.connect('/run/server.sock', {
      clock,
      connectTimeoutMs: 40,
      network,
    });

    await expect(connecting).rejects.toBe(listenerError);
    expect(network.connections[0]?.closed).toBe(true);
    expect(network.liveConnections).toEqual([]);
    expect(clock.cancellations).toBe(1);
  });

  it('destroys an initial dial when both acquisition and close observation fail', async () => {
    const network = new FakeClientNetwork();
    const listenerError = new Error('could not observe socket connection');
    network.failNextConnectListener(listenerError);
    network.failNextListener('close', new Error('could not observe terminal close'));
    const clock = new StartupClock();

    const connecting = ServerClient.connect('/run/server.sock', {
      clock,
      connectTimeoutMs: 40,
      network,
    });

    await expect(connecting).rejects.toBe(listenerError);
    expect(network.connections[0]?.closed).toBe(true);
    expect(network.connections[0]?.activeListenerCount).toBe(0);
    expect(network.liveConnections).toEqual([]);
    expect(clock.cancellations).toBe(1);
  });

  for (const [kind, acquisition] of CONTROL_LISTENER_FAILURES) {
    it(`rolls the initial control socket back when its ${kind} listener cannot be acquired`, async () => {
      const network = new FakeClientNetwork();
      const clock = new StartupClock();
      const listenerError = new Error(`control ${kind} listener failed`);
      network.failNextListener(kind, listenerError, acquisition);

      const connecting = ServerClient.connect('/run/server.sock', {
        clock,
        connectTimeoutMs: 40,
        network,
      });
      network.connections[0]?.connect();

      await expect(connecting).rejects.toBe(listenerError);
      expect(network.connections[0]?.closed).toBe(true);
      expect(network.connections[0]?.activeListenerCount).toBe(0);
      expect(network.liveConnections).toEqual([]);
      expect(clock.cancellations).toBe(1);
    });
  }

  it('releases the live control connection listeners during normal close', async () => {
    const network = new FakeClientNetwork();
    const clock = new StartupClock();
    const client = await connectedClient(network, clock);
    const controlSocket = network.connections[0];
    if (controlSocket === undefined) throw new Error('initial control socket was not opened');

    await client.close();

    expect(controlSocket.closed).toBe(true);
    expect(controlSocket.activeListenerCount).toBe(0);
    expect(network.liveConnections).toEqual([]);
  });

  it('keeps a redial owned until the replacement control listeners commit', async () => {
    const network = new FakeClientNetwork();
    const clock = new StartupClock();
    const connecting = ServerClient.connect('/run/server.sock', {
      clock,
      connectTimeoutMs: 40,
      reconnectDelaysMs: [],
      network,
    });
    network.connections[0]?.connect();
    const client = await connecting;
    network.connections[0]?.peerEnd();
    const listenerError = new Error('replacement control error listener failed');
    network.failNextListener('error', listenerError, 2);

    client.reconnect();
    const pending = client.request({type: 'query.snapshot'});
    const replacement = network.connections[1];
    if (replacement === undefined) throw new Error('control redial did not open a socket');
    replacement.connect();

    await expect(pending).rejects.toMatchObject({kind: 'disconnected'});
    expect(replacement.closed).toBe(true);
    expect(replacement.activeListenerCount).toBe(0);
    expect(network.liveConnections).toEqual([]);
    await client.close();
  });

  it('adopts a dial connection delivered during listener acquisition', async () => {
    const network = new FakeClientNetwork();
    network.connectNextWhenObserved();
    const clock = new StartupClock();

    const client = await ServerClient.connect('/run/server.sock', {
      clock,
      connectTimeoutMs: 40,
      network,
    });

    expect(client.connected).toBe(true);
    expect(network.liveConnections).toHaveLength(1);
    expect(clock.cancellations).toBe(1);
    await client.close();
    expect(network.liveConnections).toEqual([]);
  });

  it('fails closed when the graceful-close scheduler throws', async () => {
    const network = new FakeClientNetwork();
    const clock = new StartupClock();
    const client = await connectedClient(network, clock);
    clock.mode = 'throw';

    await expect(client.close()).rejects.toBe(clock.schedulerError);
    expect(network.liveConnections).toEqual([]);
  });

  it('turns a synchronously delivered handshake deadline into a typed timeout and no leak', async () => {
    const network = new FakeClientNetwork();
    const clock = new StartupClock();
    const client = await connectedClient(network, clock);
    clock.mode = 'immediate';

    const subscription = client.subscribe(
      0,
      () => undefined,
      () => undefined,
    );

    await expect(subscription).rejects.toBeInstanceOf(BackendClientError);
    await expect(subscription).rejects.toMatchObject({
      kind: 'timeout',
      message: 'Server subscription timed out after 40ms',
    });
    expect(network.connections).toHaveLength(2);
    expect(network.connections[1]?.closed).toBe(true);
    expect(network.connections[1]?.writes).toEqual([]);
    // Initial-connect cancellation plus adoption of the synchronously returned
    // subscription handle: no timer owner is lost after its callback fires.
    expect(clock.cancellations).toBe(2);

    clock.mode = 'hold';
    await client.close();
    expect(network.liveConnections).toEqual([]);
  });

  it('preserves a scheduler failure while retiring the socket it already opened', async () => {
    const network = new FakeClientNetwork();
    const clock = new StartupClock();
    const client = await connectedClient(network, clock);
    clock.mode = 'throw';

    const subscription = client.subscribe(
      0,
      () => undefined,
      () => undefined,
    );

    await expect(subscription).rejects.toBe(clock.schedulerError);
    expect(network.connections).toHaveLength(2);
    expect(network.connections[1]?.closed).toBe(true);
    expect(network.connections[1]?.writes).toEqual([]);

    clock.mode = 'hold';
    await client.close();
    expect(network.liveConnections).toEqual([]);
  });

  it('retires a subscription socket when its transport reports an error', async () => {
    const network = new FakeClientNetwork();
    const clock = new StartupClock();
    const client = await connectedClient(network, clock);
    const subscribing = client.subscribe(
      0,
      () => undefined,
      () => undefined,
    );
    const subscriptionSocket = network.connections[1];
    if (subscriptionSocket === undefined) throw new Error('subscription socket was not opened');
    subscriptionSocket.connect();

    subscriptionSocket.fail(new Error('peer reset'));

    await expect(subscribing).rejects.toMatchObject({kind: 'disconnected'});
    expect(subscriptionSocket.closed).toBe(true);
    expect(subscriptionSocket.activeListenerCount).toBe(0);
    expect(network.liveConnections).toEqual([network.connections[0]]);
    expect(clock.cancellations).toBe(2);
    await client.close();
  });

  for (const kind of OPERATION_LISTENER_KINDS) {
    it(`rolls a subscription socket back when its ${kind} listener cannot be acquired`, async () => {
      const network = new FakeClientNetwork();
      const clock = new StartupClock();
      const client = await connectedClient(network, clock);
      const listenerError = new Error(`subscription ${kind} listener failed`);
      network.failNextListener(kind, listenerError);

      const subscription = client.subscribe(
        0,
        () => undefined,
        () => undefined,
      );

      await expect(subscription).rejects.toBe(listenerError);
      const failedSocket = network.connections[1];
      expect(failedSocket?.closed).toBe(true);
      expect(failedSocket?.activeListenerCount).toBe(0);
      expect(network.liveConnections).toEqual([network.connections[0]]);
      // Only the initial dial was scheduled and cancelled; subscription
      // deadline acquisition starts after the listener transaction commits.
      expect(clock.cancellations).toBe(1);

      await client.close();
      expect(network.liveConnections).toEqual([]);
    });

    it(`rolls a dedicated socket and deadline back when its ${kind} listener cannot be acquired`, async () => {
      const network = new FakeClientNetwork();
      const clock = new StartupClock();
      const client = await connectedClient(network, clock);
      const listenerError = new Error(`dedicated ${kind} listener failed`);
      network.failNextListener(kind, listenerError);

      const request = client.request({type: 'query.chat', text: 'inspect ownership'});

      await expect(request).rejects.toBe(listenerError);
      const failedSocket = network.connections[1];
      expect(failedSocket?.closed).toBe(true);
      expect(failedSocket?.activeListenerCount).toBe(0);
      expect(network.liveConnections).toEqual([network.connections[0]]);
      // Initial dial and dedicated connect deadline each surrender exactly one
      // timer owner; no deadline remains attached to the rejected operation.
      expect(clock.cancellations).toBe(2);

      await client.close();
      expect(network.liveConnections).toEqual([]);
    });
  }

  it('rolls a dedicated socket back when its retirement error listener cannot be acquired', async () => {
    const network = new FakeClientNetwork();
    const clock = new StartupClock();
    const client = await connectedClient(network, clock);
    const listenerError = new Error('dedicated retirement error listener failed');
    // The first error listener owns the live request; the second owns its FIN
    // retirement after a complete response has arrived.
    network.failNextListener('error', listenerError, 2);
    const request = client.request({type: 'query.chat', text: 'inspect retirement'});

    answerDedicatedRequest(network);

    await expect(request).rejects.toBe(listenerError);
    expect(network.connections[1]?.closed).toBe(true);
    expect(network.connections[1]?.activeListenerCount).toBe(0);
    expect(network.liveConnections).toEqual([network.connections[0]]);
    expect(clock.cancellations).toBe(2);
    await client.close();
  });

  it('retains a completed dedicated retirement failure for terminal close', async () => {
    const network = new FakeClientNetwork();
    const clock = new StartupClock();
    const client = await connectedClient(network, clock);
    const listenerError = new Error('dedicated retirement close listener failed');
    network.failNextListener('close', listenerError);
    const request = client.request({type: 'query.chat', text: 'inspect retirement'});

    answerDedicatedRequest(network);

    await expect(request).resolves.toMatchObject({ok: true});
    const retiredSocket = network.connections[1];
    if (retiredSocket === undefined) throw new Error('dedicated request did not open a socket');
    // Wait on the Fake's causal listener-release notification rather than on
    // runtime-specific promise scheduling order.
    await retiredSocket.whenListenersReleased();
    expect(retiredSocket.closed).toBe(true);
    expect(retiredSocket.activeListenerCount).toBe(0);
    expect(network.liveConnections).toEqual([network.connections[0]]);
    await expect(client.close()).rejects.toBe(listenerError);
  });

  it('publishes a redial owner before its scheduler can close the client reentrantly', async () => {
    const network = new FakeClientNetwork();
    const clock = new ReentrantCloseClock();
    const connecting = ServerClient.connect('/run/server.sock', {
      clock,
      connectTimeoutMs: 40,
      reconnectDelaysMs: [0],
      network,
    });
    network.connections[0]?.connect();
    const client = await connecting;
    clock.client = client;

    network.connections[0]?.peerEnd();

    expect(clock.calls).toBe(3);
    const closing = clock.closeResult;
    if (closing === null) throw new Error('redial scheduler did not close the client');
    const openingSocket = network.connections[1];
    expect(openingSocket?.closed).toBe(true);
    await closing;
    expect(network.liveConnections).toEqual([]);
  });
});
