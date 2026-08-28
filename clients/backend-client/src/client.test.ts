import {afterEach, describe, expect, it} from 'bun:test';
import {randomUUID} from 'node:crypto';
import {unlink} from 'node:fs/promises';
import {createServer, type Server, type Socket} from 'node:net';
import {join} from 'node:path';
import {ServerClient, type ServerClientOptions, ServerError} from './client.js';

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
          message: 'invalid request',
          diagnostic: {
            id: 'request-1',
            summary: 'The request could not be completed.',
          },
        });
      },
    );
  });

  it('rejects pending requests when the server disconnects', async () => {
    await withServer(
      socket => socket.once('data', () => socket.destroy()),
      async client => {
        await expect(client.request({type: 'query.snapshot'})).rejects.toThrow(
          'Server disconnected',
        );
      },
    );
  });

  it('rejects malformed responses instead of throwing from the socket callback', async () => {
    await withServer(
      socket => socket.once('data', () => socket.write('{not-json}\n')),
      async client => {
        await expect(client.request({type: 'query.snapshot'})).rejects.toThrow(
          'Invalid server response JSON',
        );
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
        await expect(client.request({type: 'query.snapshot'})).rejects.toThrow(
          'Server request timed out after 20ms',
        );
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
            socket.write(
              `${JSON.stringify({
                ...successResponse(request['request_id'] as string),
                chat: {
                  question: 'what happened?',
                  answer: 'The agent finished its investigation.',
                  effect: 'none',
                },
              })}\n`,
            );
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

  it('carries a subscribe tail only when one is asked for', async () => {
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

        expect(frames[0]).toMatchObject({after_sequence: 0, tail: 1000});
        // An old server forbids unknown fields, so the plain call must not
        // carry the key at all, not even as null.
        expect(frames[1]).not.toHaveProperty('tail');
        await tailed.close();
        await full.close();
      },
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
        });
      },
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

    await expect(
      ServerClient.connect(socketPath, {connectTimeoutMs: 120, connectRetryIntervalMs: 20}),
    ).rejects.toThrow(/Timed out connecting to server after 120ms: .*ENOENT/);
  });

  it('stops retrying once the deadline passes', async () => {
    socketPath = join('/tmp', `vs-${randomUUID().slice(0, 8)}.sock`);
    const start = Date.now();

    await expect(
      ServerClient.connect(socketPath, {connectTimeoutMs: 100, connectRetryIntervalMs: 10}),
    ).rejects.toThrow();
    expect(Date.now() - start).toBeLessThan(2_000);
  });
});

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
