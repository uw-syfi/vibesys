import {afterEach, describe, expect, it} from 'bun:test';
import {randomUUID} from 'node:crypto';
import {unlink} from 'node:fs/promises';
import {createServer, type Server, type Socket} from 'node:net';
import {join} from 'node:path';
import {BackendClientError, type ControlChannelState, ServerError} from '../index.js';
import {ServerClient, type ServerClientOptions} from './client.js';

/** One reported state as a short string; see `websocket.test.ts`'s `trace`. */
function traceState(state: ControlChannelState): string {
  if (state.status === 'connected') return 'connected';
  return `down:${state.everConnected ? 'lost' : 'cold'}${state.retrying ? ':retrying' : ''}`;
}

/**
 * The only report that may immediately precede each one, which is the channel's
 * transition graph written down: down, dialing, up, and round again.
 */
const PREDECESSOR: Record<string, string> = {
  'down:lost': 'connected',
  'down:lost:retrying': 'down:lost',
  connected: 'down:lost:retrying',
};

/**
 * A one-shot event a test can await, so a wait is woken by the thing it waits
 * for rather than by a poll interval.
 */
class Signal {
  #fire: () => void = () => undefined;
  readonly fired = new Promise<void>(resolve => {
    this.#fire = resolve;
  });

  fire(): void {
    this.#fire();
  }
}

/**
 * Collect the channel's connectivity reports and let a test wait on them.
 *
 * These tests drive a real socket, so recovery finishes when the OS delivers a
 * connection, not when a timer the client armed expires. Polling `connected`
 * against a wall-clock deadline therefore fails spuriously on a loaded machine
 * and no injectable clock fixes it, because there is no client-side timer to
 * inject. Waking on the report the channel already emits is exact: `#install`
 * publishes `connected` in the same turn it takes the connection, so the
 * predicate is true whenever this resolves.
 *
 * Deliberately unbounded. The bound is the runner's own per-test timeout, the
 * same one every `await client.request(...)` in this file relies on, and unlike
 * the poll loop it cannot expire while the connection is still on its way.
 */
class ConnectionWatcher {
  readonly states: ControlChannelState[] = [];
  #waiting: Array<() => void> = [];

  readonly observe = (state: ControlChannelState): void => {
    this.states.push(state);
    const waiting = this.#waiting;
    this.#waiting = [];
    for (const resolve of waiting) resolve();
  };

  /** Resolve once the client holds a live control connection. */
  async live(client: ServerClient): Promise<void> {
    while (!client.connected) {
      await new Promise<void>(resolve => this.#waiting.push(resolve));
    }
  }

  trace(): string[] {
    return this.states.map(traceState);
  }
}

let socketPath: string | undefined;

afterEach(async () => {
  if (socketPath) await unlink(socketPath).catch(() => undefined);
  socketPath = undefined;
});

describe('ServerClient', () => {
  it('reassembles a response fragmented across socket chunks', async () => {
    await withServer(
      socket =>
        respondToLines(socket, request => {
          const response = JSON.stringify(successResponse(request['request_id'] as string));
          const middle = Math.floor(response.length / 2);
          socket.write(response.slice(0, middle));
          socket.write(`${response.slice(middle)}\n`);
        }),
      async client => {
        const response = await client.request({type: 'query.snapshot'});
        expect(response.snapshot?.status).toBe('running');
      },
    );
  });

  it('correlates concurrent responses received out of order', async () => {
    await withServer(
      socket => {
        const requests: Array<Record<string, unknown>> = [];
        // biome-ignore lint/complexity/noExcessiveCognitiveComplexity: pre-existing; tracked: #288
        respondToLines(socket, request => {
          requests.push(request);
          if (requests.length !== 2) return;
          for (const item of [...requests].reverse()) {
            const action = item['type'] === 'command.pause' ? 'pause' : 'resume';
            socket.write(
              `${JSON.stringify({
                ...successResponse(item['request_id'] as string),
                ack: {action, status: action === 'pause' ? 'pending' : 'consumed'},
              })}\n`,
            );
          }
        });
      },
      async client => {
        const pause = client.request({type: 'command.pause', mode: 'after_current_agent_call'});
        const resume = client.request({type: 'command.resume'});
        await expect(pause).resolves.toMatchObject({ack: {action: 'pause'}});
        await expect(resume).resolves.toMatchObject({ack: {action: 'resume'}});
      },
    );
  });

  it('rejects structured backend errors', async () => {
    await withServer(
      socket =>
        respondToLines(socket, request => {
          socket.write(
            `${JSON.stringify({
              protocol_version: 1,
              request_id: request['request_id'],
              timestamp: new Date().toISOString(),
              ok: false,
              error: 'invalid request',
              diagnostic: {
                id: 'request-1',
                code: 'future_backend_code',
                summary: 'The request could not be completed.',
                detail: 'The backend rejected the command.',
                hint: 'Retry after checking the run state.',
                scope: 'request',
                severity: 'error',
                retryability: 'manual',
              },
              events: [],
            })}\n`,
          );
        }),
      async client => {
        const rejected = client.request({type: 'query.snapshot'});
        await expect(rejected).rejects.toBeInstanceOf(ServerError);
        await expect(rejected).rejects.toMatchObject({
          name: 'ServerError',
          kind: 'rejected',
          retryable: false,
          message: 'invalid request',
          diagnostic: {
            id: 'request-1',
            summary: 'The request could not be completed.',
          },
        });
      },
    );
  });

  it('fails an in-flight request once the reconnect schedule cannot recover', async () => {
    await withServer(
      socket => socket.once('data', () => socket.destroy()),
      async client => {
        // An idempotent request is held for resend across a drop; with no
        // schedule to redial on, the outage exhausts at once and it fails typed.
        const rejected = client.request({type: 'query.snapshot'});
        await expect(rejected).rejects.toThrow('Server is disconnected');
        await expect(rejected).rejects.toMatchObject({kind: 'disconnected', retryable: true});
      },
      {reconnectDelaysMs: []},
    );
  });

  it('rejects malformed responses instead of throwing from the socket callback', async () => {
    await withServer(
      socket => socket.once('data', () => socket.write('{not-json}\n')),
      async client => {
        const rejected = client.request({type: 'query.snapshot'});
        await expect(rejected).rejects.toThrow('Invalid server response JSON');
        await expect(rejected).rejects.toMatchObject({kind: 'parse', retryable: false});
      },
    );
  });

  it('rejects incompatible protocol versions', async () => {
    await withServer(
      socket =>
        respondToLines(socket, request => {
          socket.write(
            `${JSON.stringify({...successResponse(request['request_id'] as string), protocol_version: 2})}\n`,
          );
        }),
      async client => {
        await expect(client.request({type: 'query.snapshot'})).rejects.toThrow(
          'Unsupported server protocol version',
        );
      },
    );
  });

  it('times out requests that never receive a response', async () => {
    await withServer(
      socket => socket.on('data', () => undefined),
      async client => {
        const rejected = client.request({type: 'query.snapshot'});
        await expect(rejected).rejects.toThrow('Server request timed out after 20ms');
        await expect(rejected).rejects.toMatchObject({kind: 'timeout', retryable: true});
      },
      {requestTimeoutMs: 20},
    );
  });

  it('runs long agent chat on a dedicated connection without a response timeout', async () => {
    let connections = 0;
    let resolveChatSocketClosed: (() => void) | undefined;
    const chatSocketClosed = new Promise<void>(resolve => {
      resolveChatSocketClosed = resolve;
    });
    await withServer(
      socket => {
        connections += 1;
        respondToLines(socket, request => {
          if (request['type'] !== 'query.chat') return;
          socket.once('close', () => resolveChatSocketClosed?.());
          setTimeout(() => {
            const response = JSON.stringify({
              ...successResponse(request['request_id'] as string),
              chat: {
                question: 'what happened?',
                answer: 'The agent finished its investigation.',
                effect: 'none',
              },
            });
            const middle = Math.floor(response.length / 2);
            socket.write(response.slice(0, middle));
            socket.write(`${response.slice(middle)}\n`);
          }, 50);
        });
      },
      async client => {
        const chat = client.request({type: 'query.chat', text: 'what happened?'});
        await expect(client.request({type: 'query.snapshot'})).rejects.toThrow(
          'Server request timed out after 20ms',
        );
        const response = await chat;
        expect(response.chat?.answer).toBe('The agent finished its investigation.');
        expect(connections).toBe(2);
        await chatSocketClosed;
      },
      {requestTimeoutMs: 20},
    );
  });

  it('rejects malformed responses on a long-running connection', async () => {
    await withServer(
      socket =>
        respondToLines(socket, request => {
          if (request['type'] === 'query.chat') socket.write('{not-json}\n');
        }),
      async client => {
        await expect(client.request({type: 'query.chat', text: 'what happened?'})).rejects.toThrow(
          'Invalid server response JSON',
        );
      },
    );
  });

  it('reassembles and validates fragmented subscription messages', async () => {
    await withServer(
      socket =>
        respondToLines(socket, request => {
          if (request['type'] !== 'subscribe') return;
          const subscribed = `${JSON.stringify({
            type: 'subscribed',
            request_id: request['request_id'],
            run_id: 'run-1',
            latest_sequence: 1,
          })}\n`;
          const batch = `${JSON.stringify({
            type: 'event_batch',
            events: [{sequence: 1, timestamp: new Date().toISOString(), type: 'server_started'}],
          })}\n`;
          socket.write(subscribed.slice(0, 10));
          socket.write(`${subscribed.slice(10)}${batch}`);
        }),
      async client => {
        const messages: string[] = [];
        const subscription = await client.subscribe(
          0,
          message => messages.push(String(message.type)),
          error => {
            throw error;
          },
        );
        expect(messages).toEqual(['subscribed', 'event_batch']);
        await subscription.close();
      },
    );
  });

  it('carries a subscribe tail and store id only when one is asked for', async () => {
    const frames: Array<Record<string, unknown>> = [];
    await withServer(
      socket =>
        respondToLines(socket, request => {
          if (request['type'] !== 'subscribe') return;
          frames.push(request);
          socket.write(
            `${JSON.stringify({
              type: 'subscribed',
              request_id: request['request_id'],
              run_id: 'run-1',
              latest_sequence: 0,
            })}\n`,
          );
        }),
      async client => {
        const tailed = await client.subscribe(0, () => undefined, noopDisconnect, {tail: 1000});
        const full = await client.subscribe(0, () => undefined, noopDisconnect);
        const named = await client.subscribe(3, () => undefined, noopDisconnect, {storeId: 'log'});
        const unnamed = await client.subscribe(3, () => undefined, noopDisconnect, {storeId: ''});

        expect(frames[0]).toMatchObject({after_sequence: 0, tail: 1000});
        expect(frames.map(frame => frame['client_id'])).toEqual([
          'node-client',
          'node-client',
          'node-client',
          'node-client',
        ]);
        // An old server forbids unknown fields, so the plain call must not
        // carry the key at all, not even as null.
        expect(frames[1]).not.toHaveProperty('tail');
        // `subscribeRequest` owns both encodings, so this transport and the
        // browser one agree: a named store rides along, an empty one is
        // absence, and neither ever becomes an explicit null.
        expect(frames[2]).toMatchObject({after_sequence: 3, store_id: 'log'});
        expect(frames[3]).not.toHaveProperty('store_id');
        await tailed.close();
        await full.close();
        await named.close();
        await unnamed.close();
      },
      {clientId: 'node-client'},
    );
  });

  it('reports an event-stream disconnect only once', async () => {
    await withServer(
      socket =>
        respondToLines(socket, request => {
          if (request['type'] !== 'subscribe') return;
          socket.write(
            `${JSON.stringify({
              type: 'subscribed',
              request_id: request['request_id'],
              run_id: 'run-1',
              latest_sequence: 0,
            })}\n`,
            () => socket.destroy(),
          );
        }),
      async client => {
        const disconnects: Error[] = [];
        await client.subscribe(
          0,
          () => undefined,
          error => disconnects.push(error),
        );
        await new Promise(resolve => setTimeout(resolve, 20));
        expect(disconnects).toHaveLength(1);
      },
    );
  });

  it('keeps a structured protocol error when the stream closes afterward', async () => {
    await withServer(
      socket =>
        respondToLines(socket, request => {
          if (request['type'] !== 'subscribe') return;
          socket.write(
            `${JSON.stringify({
              type: 'subscribed',
              request_id: request['request_id'],
              run_id: 'run-1',
              latest_sequence: 0,
            })}\n${JSON.stringify({
              type: 'protocol_error',
              code: 'stream_failed',
              message: 'Event stream failed',
              diagnostic: {
                id: 'stream-1',
                code: 'stream_failed',
                summary: 'Event stream failed',
                detail: 'RuntimeError: event store is unavailable',
                scope: 'protocol',
                severity: 'error',
              },
            })}\n`,
            () => socket.destroy(),
          );
        }),
      async client => {
        const messages: string[] = [];
        const disconnects: Error[] = [];
        await client.subscribe(
          0,
          message => messages.push(String(message.type)),
          error => disconnects.push(error),
        );
        await new Promise(resolve => setTimeout(resolve, 20));

        expect(messages).toEqual(['subscribed', 'protocol_error']);
        expect(disconnects).toEqual([]);
      },
    );
  });

  it('reports unknown event-stream message types as protocol errors', async () => {
    await withServer(
      socket =>
        respondToLines(socket, request => {
          if (request['type'] !== 'subscribe') return;
          socket.write(
            `${JSON.stringify({
              type: 'subscribed',
              request_id: request['request_id'],
              run_id: 'run-1',
              latest_sequence: 0,
            })}\n${JSON.stringify({type: 'unknown'})}\n`,
          );
        }),
      async client => {
        const disconnect = new Promise<Error>(resolve => {
          void client.subscribe(0, () => undefined, resolve);
        });
        await expect(disconnect).resolves.toMatchObject({
          message: expect.stringContaining('Unknown server event-stream message'),
          kind: 'parse',
          retryable: false,
        });
      },
    );
  });

  it('rejects the subscription as a server rejection on an old-server Response line', async () => {
    await withServer(
      socket =>
        respondToLines(socket, request => {
          if (request['type'] !== 'subscribe') return;
          // An old server rejects a subscribe carrying an unknown field the
          // way it rejects any request: a Response line on the same socket.
          socket.write(
            `${JSON.stringify({
              protocol_version: 1,
              request_id: request['request_id'],
              timestamp: new Date().toISOString(),
              ok: false,
              error: 'Extra inputs are not permitted: tail',
              diagnostic: {
                id: 'request-1',
                code: 'invalid_request',
                summary: 'Extra inputs are not permitted: tail',
                scope: 'request',
                severity: 'error',
              },
            })}\n`,
            () => socket.end(),
          );
        }),
      async client => {
        const rejected = client.subscribe(0, () => undefined, noopDisconnect, {tail: 1_000});
        await expect(rejected).rejects.toBeInstanceOf(ServerError);
        await expect(rejected).rejects.toMatchObject({
          name: 'ServerError',
          kind: 'rejected',
          retryable: false,
          message: 'Extra inputs are not permitted: tail',
          diagnostic: {id: 'request-1', code: 'invalid_request'},
        });
      },
    );
  });

  it('rejects the subscription with the refusal when a protocol error precedes the handshake', async () => {
    await withServer(
      socket =>
        respondToLines(socket, request => {
          if (request['type'] !== 'subscribe') return;
          socket.write(
            `${JSON.stringify({
              type: 'protocol_error',
              code: 'stream_failed',
              message: 'Event stream failed',
              diagnostic: {
                id: 'stream-1',
                code: 'stream_failed',
                summary: 'Event stream failed',
                scope: 'protocol',
                severity: 'error',
              },
            })}\n`,
            () => socket.end(),
          );
        }),
      async client => {
        const messages: string[] = [];
        const rejected = client.subscribe(
          0,
          message => messages.push(String(message.type)),
          noopDisconnect,
        );
        await expect(rejected).rejects.toBeInstanceOf(ServerError);
        await expect(rejected).rejects.toMatchObject({
          kind: 'rejected',
          retryable: false,
          message: 'Event stream failed',
          diagnostic: {id: 'stream-1'},
        });
        // The refusal is still delivered as a message, as it is after the
        // handshake, so consumers see one shape wherever it lands.
        expect(messages).toEqual(['protocol_error']);
      },
    );
  });

  it('types a stream that closes before the handshake as a transport disconnect', async () => {
    await withServer(
      socket => socket.once('data', () => socket.end()),
      async client => {
        const rejected = client.subscribe(0, () => undefined, noopDisconnect);
        await expect(rejected).rejects.toBeInstanceOf(BackendClientError);
        await expect(rejected).rejects.toMatchObject({
          kind: 'disconnected',
          retryable: true,
          message: 'Server event stream disconnected before subscription',
        });
      },
    );
  });

  it('types the subscription handshake timeout', async () => {
    await withServer(
      socket => socket.on('data', () => undefined),
      async client => {
        await expect(client.subscribe(0, () => undefined, noopDisconnect)).rejects.toMatchObject({
          name: 'BackendClientError',
          kind: 'timeout',
          retryable: true,
          message: 'Server subscription timed out after 40ms',
        });
      },
      {connectTimeoutMs: 40},
    );
  });

  it('keeps retrying until a socket that does not exist yet accepts', async () => {
    socketPath = join('/tmp', `vs-${randomUUID().slice(0, 8)}.sock`);
    const server = createServer(socket =>
      respondToLines(socket, request =>
        socket.write(`${JSON.stringify(successResponse(request['request_id'] as string))}\n`),
      ),
    );
    const path = socketPath;
    const connecting = ServerClient.connect(path, {connectTimeoutMs: 5_000});
    setTimeout(() => void listen(server, path), 150);

    const client = await connecting;
    try {
      await expect(client.request({type: 'query.snapshot'})).resolves.toMatchObject({ok: true});
    } finally {
      await client.close();
      await close(server);
    }
  });

  it('reports the last connection failure when the backend never listens', async () => {
    socketPath = join('/tmp', `vs-${randomUUID().slice(0, 8)}.sock`);

    const rejected = ServerClient.connect(socketPath, {
      connectTimeoutMs: 120,
      connectRetryIntervalMs: 20,
    });
    await expect(rejected).rejects.toThrow(/Timed out connecting to server after 120ms: .*ENOENT/);
    await expect(rejected).rejects.toMatchObject({kind: 'timeout', retryable: true});
  });

  it('stops retrying once the deadline passes', async () => {
    socketPath = join('/tmp', `vs-${randomUUID().slice(0, 8)}.sock`);
    const start = Date.now();

    await expect(
      ServerClient.connect(socketPath, {connectTimeoutMs: 100, connectRetryIntervalMs: 10}),
    ).rejects.toThrow();
    expect(Date.now() - start).toBeLessThan(2_000);
  });

  it('reports a peer close that the write racing it hides', async () => {
    // A request issued in the window between the peer's FIN and the transport
    // noticing it is written onto a half-closed socket, where it neither fails
    // nor arrives. On the pinned Bun (1.3.9) that write also suppresses the
    // socket's own `'close'`, so a transport that waits for `'close'` has
    // nothing left to learn the outage from: the connection keeps reporting
    // itself live and the request waits out its full response deadline.
    let connections = 0;
    const states: string[] = [];
    await withServer(
      socket => {
        connections += 1;
        const attempt = connections;
        socket.on('error', () => undefined);
        respondToLines(socket, request => {
          const response = `${JSON.stringify(successResponse(request['request_id'] as string))}\n`;
          // The first connection answers and ends in one call, so the FIN lands
          // together with the bytes the caller was waiting for. The second
          // stays open, so the recovery settles and the states below are final.
          if (attempt === 1) socket.end(response);
          else socket.write(response);
        });
      },
      async client => {
        await expect(client.request({type: 'command.pause'})).resolves.toMatchObject({ok: true});
        // Issued with no wait, so it races the close either way round. The
        // caller gets an answer regardless: the outage is reported, and a
        // repeatable request rides the redial rather than the dead socket.
        await expect(client.request({type: 'command.pause'})).resolves.toMatchObject({ok: true});

        // The outage is reported before the recovery is, and one redial covers
        // it: the dial count is what says nothing reconnected behind the
        // report. How many times an outage reports itself is the channel's
        // business, so it is not pinned here.
        expect(states[0]).toBe('disconnected');
        expect(states.at(-1)).toBe('connected');
        expect(connections).toBe(2);
      },
      {reconnectDelaysMs: [0], onConnectionState: state => states.push(state.status)},
    );
  });

  it('close() tears down a live subscription without reporting it as an outage', async () => {
    let disconnects = 0;
    await withServer(
      socket =>
        respondToLines(socket, request => {
          if (request['type'] === 'subscribe') socket.write(subscribedMessage(request));
        }),
      async client => {
        // A subscription the client owns but the caller never closed itself.
        await client.subscribe(
          0,
          () => undefined,
          () => {
            disconnects += 1;
          },
        );
        await client.close();
        // Closing the client destroyed the subscription socket, but a client-wide
        // close is not a stream outage, so its disconnect handler never fired. This
        // holds whichever order a caller closes the stream and the client in.
        expect(disconnects).toBe(0);
      },
      {closeGraceMs: 30},
    );
  });

  it('close() resolves within the grace deadline when the server never closes', async () => {
    socketPath = join('/tmp', `vs-${randomUUID().slice(0, 8)}.sock`);
    // Accept the connection and then ignore it forever: never respond, never end.
    // The client's graceful end gets no FIN back, so only the destroy deadline can
    // resolve close(). The pre-fix close() waited on 'close' unconditionally and
    // would hang here.
    const server = createServer(() => undefined);
    await listen(server, socketPath);
    const client = await ServerClient.connect(socketPath, {closeGraceMs: 40});
    try {
      const start = Date.now();
      await client.close();
      expect(Date.now() - start).toBeLessThan(1_000);
    } finally {
      await close(server);
    }
  });

  it('surfaces an unframable event stream as a parse disconnect', async () => {
    await withServer(
      socket =>
        respondToLines(socket, request => {
          if (request['type'] !== 'subscribe') return;
          socket.write(subscribedMessage(request));
          // A peer that has stopped delimiting frames: bytes without a newline,
          // past the framer's remainder cap.
          socket.write('x'.repeat(4 * 1024 * 1024 + 1));
        }),
      async client => {
        const error = await new Promise<Error>(resolve => {
          void client.subscribe(0, () => undefined, resolve);
        });
        expect(error).toBeInstanceOf(BackendClientError);
        expect((error as BackendClientError).kind).toBe('parse');
      },
      {closeGraceMs: 30},
    );
  });

  it('rejects a pending request when the control stream cannot be framed', async () => {
    await withServer(
      socket => {
        // The client releases the connection on the framing error, so swallow
        // whatever the server sees rather than let it become an uncaught error.
        socket.on('error', () => undefined);
        socket.once('data', () => socket.write('x'.repeat(4 * 1024 * 1024 + 1)));
      },
      async client => {
        const rejected = client.request({type: 'query.snapshot'});
        await expect(rejected).rejects.toMatchObject({kind: 'parse'});
      },
    );
  });

  it('close() abandons an in-flight chat as a disconnect', async () => {
    await withServer(
      socket => {
        // close() destroys the chat socket, so swallow the reset the server sees.
        socket.on('error', () => undefined);
        // Accept the chat connection but never answer, so it is still in flight.
        respondToLines(socket, () => undefined);
      },
      async client => {
        // Capture the rejection as a value so close() abandoning the chat does
        // not surface as an unhandled rejection before it is awaited.
        const chat = client.request({type: 'query.chat', text: 'hang'}).catch(error => error);
        await client.close();
        expect((await chat).kind).toBe('disconnected');
      },
      {closeGraceMs: 30},
    );
  });

  it('redials the control channel and does not resend a non-idempotent request', async () => {
    let connections = 0;
    const seen: string[] = [];
    const watcher = new ConnectionWatcher();
    await withServer(
      socket => {
        connections += 1;
        const attempt = connections;
        socket.on('error', () => undefined);
        respondToLines(socket, request => {
          seen.push(request['type'] as string);
          if (attempt === 1) {
            socket.destroy();
            return;
          }
          socket.write(`${JSON.stringify(successResponse(request['request_id'] as string))}\n`);
        });
      },
      async client => {
        // A steer is not idempotent, so the drop fails it rather than resending.
        await expect(client.request({type: 'command.steer', text: 'x'})).rejects.toMatchObject({
          kind: 'disconnected',
        });
        await watcher.live(client);
        const response = await client.request({type: 'query.snapshot'});
        expect(response.snapshot?.status).toBe('running');
        // The steer reached only the first connection; the redial did not repeat it.
        expect(seen).toEqual(['command.steer', 'query.snapshot']);
        // `everConnected` is true from the first report: the Node client is
        // handed an open socket and adopts it, so it is never cold-started.
        // The redial's own start is reported between the drop and the recovery.
        expect(watcher.trace()).toEqual(['down:lost', 'down:lost:retrying', 'connected']);
      },
      {reconnectDelaysMs: [0], onConnectionState: watcher.observe},
    );
  });

  it('holds an idempotent request across a drop and resends it on recovery', async () => {
    let connections = 0;
    await withServer(
      socket => {
        connections += 1;
        const attempt = connections;
        socket.on('error', () => undefined);
        if (attempt === 1) {
          // Drop the request without answering, so it must ride the recovery.
          socket.once('data', () => socket.destroy());
          return;
        }
        respondToLines(socket, request =>
          socket.write(`${JSON.stringify(successResponse(request['request_id'] as string))}\n`),
        );
      },
      async client => {
        const response = await client.request({type: 'command.pause'});
        expect(response.ok).toBe(true);
        expect(connections).toBeGreaterThanOrEqual(2);
      },
      {reconnectDelaysMs: [0]},
    );
  });

  it('resolves an in-flight request whose response arrives before the drop', async () => {
    await withServer(
      socket => {
        socket.on('error', () => undefined);
        respondToLines(socket, request => {
          // Answer, then immediately drop: the response wins the race.
          socket.write(`${JSON.stringify(successResponse(request['request_id'] as string))}\n`);
          socket.destroy();
        });
      },
      async client => {
        const response = await client.request({type: 'query.snapshot'});
        expect(response.snapshot?.status).toBe('running');
      },
      {reconnectDelaysMs: []},
    );
  });

  it('discards the late response of an aborted request and stays routable', async () => {
    let serverSocket: Socket | undefined;
    const deferred: Record<string, unknown>[] = [];
    const arrived = new Signal();
    await withServer(
      socket => {
        serverSocket = socket;
        socket.on('error', () => undefined);
        respondToLines(socket, request => {
          if (request['type'] === 'query.snapshot') {
            // Hold the snapshot answer back; the caller aborts before it lands.
            deferred.push(request);
            arrived.fire();
            return;
          }
          socket.write(`${JSON.stringify(successResponse(request['request_id'] as string))}\n`);
        });
      },
      async client => {
        const controller = new AbortController();
        const aborted = client
          .request({type: 'query.snapshot'}, {signal: controller.signal})
          .catch(error => error);
        // Woken by the server receiving the request, not by a poll interval:
        // the abort has to land while the request is genuinely on the wire.
        await arrived.fired;
        expect(deferred).toHaveLength(1);
        controller.abort();
        expect((await aborted).name).toBe('AbortError');
        // The abandoned request's late response must resolve nothing.
        serverSocket?.write(
          `${JSON.stringify(successResponse(deferred[0]?.['request_id'] as string))}\n`,
        );
        // A later request on the same channel still works and is not misrouted.
        const response = await client.request({type: 'command.pause'});
        expect(response.ok).toBe(true);
      },
      {reconnectDelaysMs: []},
    );
  });

  it('aborts an in-flight dedicated chat with the abort reason', async () => {
    await withServer(
      socket => {
        socket.on('error', () => undefined);
        // Accept the chat connection but never answer, so it is still in flight.
        respondToLines(socket, () => undefined);
      },
      async client => {
        const controller = new AbortController();
        const chat = client
          .request({type: 'query.chat', text: 'hang'}, {signal: controller.signal})
          .catch(error => error);
        controller.abort();
        expect((await chat).name).toBe('AbortError');
      },
      {closeGraceMs: 30},
    );
  });

  it('routes a request onto a dedicated connection with no deadline when asked', async () => {
    let connections = 0;
    await withServer(
      socket => {
        connections += 1;
        socket.on('error', () => undefined);
        respondToLines(socket, request => {
          // Answer only after the control deadline would have fired; the no-timer
          // dedicated path is the only one that survives it.
          setTimeout(
            () =>
              socket.write(`${JSON.stringify(successResponse(request['request_id'] as string))}\n`),
            40,
          );
        });
      },
      async client => {
        const response = await client.request(
          {type: 'query.snapshot'},
          {dedicatedConnection: true},
        );
        expect(response.snapshot?.status).toBe('running');
        expect(connections).toBe(2);
      },
      {requestTimeoutMs: 20},
    );
  });

  it('honors a per-call timeout override', async () => {
    await withServer(
      socket => socket.on('data', () => undefined),
      async client => {
        // The client default is 30s; the per-call override must win.
        const rejected = client.request({type: 'query.snapshot'}, {timeoutMs: 20});
        await expect(rejected).rejects.toThrow('Server request timed out after 20ms');
      },
    );
  });

  it('fails a non-idempotent request issued while the control channel is down', async () => {
    let connections = 0;
    const seen: string[] = [];
    await withServer(
      socket => {
        connections += 1;
        socket.on('error', () => undefined);
        respondToLines(socket, request => {
          seen.push(request['type'] as string);
          socket.write('{not-json}\n');
        });
      },
      async client => {
        // A protocol fault takes the channel down with no redial pending: a
        // redial would fetch the same unreadable bytes back.
        await expect(client.request({type: 'query.snapshot'})).rejects.toMatchObject({
          kind: 'parse',
        });
        expect(client.connected).toBe(false);
        const dialsBefore = connections;

        // A steer issued against that state must fail on the spot. Queueing it
        // for a later revive would apply, seconds later and invisibly, a
        // command the caller was already told could not be delivered.
        await expect(client.request({type: 'command.steer', text: 'x'})).rejects.toMatchObject({
          kind: 'disconnected',
          retryable: true,
        });
        expect(connections).toBe(dialsBefore);
        expect(seen).toEqual(['query.snapshot']);
      },
      // A schedule is configured, so the refusal is the down state's answer and
      // not an artifact of having nothing to redial on.
      {reconnectDelaysMs: [0]},
    );
  });

  it('revives a spent channel for a repeatable request but still refuses a steer', async () => {
    let connections = 0;
    const seen: string[] = [];
    await withServer(
      socket => {
        connections += 1;
        const attempt = connections;
        socket.on('error', () => undefined);
        if (attempt === 1) {
          socket.once('data', () => socket.destroy());
          return;
        }
        respondToLines(socket, request => {
          seen.push(request['type'] as string);
          socket.write(`${JSON.stringify(successResponse(request['request_id'] as string))}\n`);
        });
      },
      async client => {
        // The drop spends the empty schedule at once, so the channel is down
        // with nothing pending, and the caller has been told so.
        await expect(client.request({type: 'query.snapshot'})).rejects.toMatchObject({
          kind: 'disconnected',
        });
        expect(client.connected).toBe(false);

        // A steer must not wait out a channel the caller was told is dead: it
        // fails on the spot and opens nothing. The Node client is connected
        // before it is constructed, so it is never in the cold-start state that
        // makes a first request of any type wait.
        await expect(client.request({type: 'command.steer', text: 'x'})).rejects.toMatchObject({
          kind: 'disconnected',
        });
        expect(connections).toBe(1);

        // A repeatable request is what asks for the connection back, so a spent
        // schedule is not a permanent death sentence for the client.
        const response = await client.request({type: 'command.pause'});
        expect(response.ok).toBe(true);
        expect(seen).toEqual(['command.pause']);
        expect(connections).toBe(2);
      },
      {reconnectDelaysMs: []},
    );
  });

  it('recovers repeated outages on a finite schedule', async () => {
    let connections = 0;
    const watcher = new ConnectionWatcher();
    await withServer(
      socket => {
        connections += 1;
        socket.on('error', () => undefined);
        respondToLines(socket, request => {
          // Answer, then end: every round costs one whole outage, so a schedule
          // whose cursor survived the previous one would strand a later request.
          socket.end(`${JSON.stringify(successResponse(request['request_id'] as string))}\n`);
        });
      },
      async client => {
        for (let round = 0; round < 4; round += 1) {
          await watcher.live(client);
          const response = await client.request({type: 'command.pause'});
          expect({round, ok: response.ok}).toEqual({round, ok: true});
        }
        expect(connections).toBeGreaterThanOrEqual(4);

        // Properties over the whole run rather than one expected sequence, so
        // they hold however the last round's drop interleaves with the loop
        // exiting, and they are checked against four real outages instead of a
        // hand-built trace.
        const trace = watcher.trace();
        // The adopted connection is not announced, so the first thing a caller
        // ever hears is the first outage.
        expect(trace[0]).toBe('down:lost');
        for (const [index, state] of trace.entries()) {
          if (index === 0) continue;
          const previous = trace[index - 1];
          // Well-formedness, which subsumes the dedup: a recovery is reported
          // only out of a dial that was reported in flight, a dial in flight
          // only out of a reported outage, and an outage only out of a live
          // channel. No state can therefore repeat its predecessor.
          expect({index, state, previous}).toEqual({
            index,
            state,
            previous: PREDECESSOR[state],
          });
        }
        // Liveness: every outage was recovered from, bar at most the one the
        // last round may still be in when the loop exits.
        const recoveries = trace.filter(state => state === 'connected').length;
        const outages = trace.filter(state => state === 'down:lost').length;
        expect({outages, unrecovered: outages - recoveries}).toEqual({
          outages,
          unrecovered: trace.at(-1) === 'connected' ? 0 : 1,
        });
      },
      {reconnectDelaysMs: [0], onConnectionState: watcher.observe},
    );
  });

  it('rejects a dedicated request whose signal is already aborted', async () => {
    let connections = 0;
    await withServer(
      socket => {
        connections += 1;
        socket.on('error', () => undefined);
        respondToLines(socket, () => undefined);
      },
      async client => {
        const controller = new AbortController();
        controller.abort();
        const dialsBefore = connections;
        const rejected = client
          .request({type: 'query.chat', text: 'x'}, {signal: controller.signal})
          .catch(error => error);

        // An already-aborted signal never fires its event, so the refusal has
        // to happen before the request is routed anywhere.
        expect((await rejected).name).toBe('AbortError');
        expect(connections).toBe(dialsBefore);
      },
    );
  });

  it('reconnect() revives a channel that a protocol fault took down', async () => {
    let connections = 0;
    const watcher = new ConnectionWatcher();
    await withServer(
      socket => {
        connections += 1;
        const attempt = connections;
        socket.on('error', () => undefined);
        respondToLines(socket, request => {
          if (attempt === 1) {
            // An unreadable response is a protocol fault, not a transient drop:
            // it fails the request typed and stays down until asked to revive.
            socket.write('{not-json}\n');
            return;
          }
          socket.write(`${JSON.stringify(successResponse(request['request_id'] as string))}\n`);
        });
      },
      async client => {
        await expect(client.request({type: 'query.snapshot'})).rejects.toMatchObject({
          kind: 'parse',
        });
        expect(client.connected).toBe(false);
        client.reconnect();
        await watcher.live(client);
        const response = await client.request({type: 'query.snapshot'});
        expect(response.snapshot?.status).toBe('running');
        // A fault is reported as an outage like any other, and the explicit
        // revive is what the recovery comes out of.
        expect(watcher.trace()).toEqual(['down:lost', 'down:lost:retrying', 'connected']);
      },
      {reconnectDelaysMs: [0], onConnectionState: watcher.observe},
    );
  });
});

/** Run one test against a fresh server and client, and tear both down after. */
async function withServer(
  onConnection: (socket: Socket) => void,
  test: (client: ServerClient) => Promise<void>,
  options: ServerClientOptions = {},
): Promise<void> {
  socketPath = join('/tmp', `vs-${randomUUID().slice(0, 8)}.sock`);
  const server = createServer(onConnection);
  await listen(server, socketPath);
  const client = await ServerClient.connect(socketPath, options);
  try {
    await test(client);
  } finally {
    await client.close();
    await close(server);
  }
}

/** A subscription the test closes itself, so a disconnect is not a failure. */
function noopDisconnect(): void {}

function respondToLines(socket: Socket, respond: (request: Record<string, unknown>) => void): void {
  let buffer = '';
  socket.setEncoding('utf8');
  socket.on('data', chunk => {
    buffer += chunk;
    const lines = buffer.split('\n');
    buffer = lines.pop() ?? '';
    for (const line of lines) {
      if (line) respond(JSON.parse(line) as Record<string, unknown>);
    }
  });
}

function subscribedMessage(request: Record<string, unknown>): string {
  return `${JSON.stringify({
    type: 'subscribed',
    request_id: request['request_id'],
    run_id: 'test',
    latest_sequence: 0,
  })}\n`;
}

function successResponse(requestId: string): Record<string, unknown> {
  return {
    protocol_version: 1,
    request_id: requestId,
    timestamp: new Date().toISOString(),
    ok: true,
    snapshot: {
      protocol_version: 1,
      run_id: 'test',
      sequence: 1,
      status: 'running',
    },
    events: [],
  };
}

function listen(server: Server, path: string): Promise<void> {
  return new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(path, resolve);
  });
}

function close(server: Server): Promise<void> {
  return new Promise((resolve, reject) => {
    server.close(error => (error ? reject(error) : resolve()));
  });
}
