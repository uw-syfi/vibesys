import {afterEach, describe, expect, it} from 'bun:test';
import {randomUUID} from 'node:crypto';
import {unlink} from 'node:fs/promises';
import {createServer, type Server, type Socket} from 'node:net';
import {join} from 'node:path';
import {ServerClient, type ServerClientOptions, ServerError} from './client.js';
import {CommandAction, RunStatus} from './protocol.js';

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
        const response = await client.request({case: 'snapshot', value: {}});
        expect(response.snapshot?.status).toBe(RunStatus.RUNNING);
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
            const action =
              item['pause'] === undefined ? 'COMMAND_ACTION_RESUME' : 'COMMAND_ACTION_PAUSE';
            socket.write(
              `${JSON.stringify({
                ...successResponse(item['request_id'] as string),
                ack: {
                  action,
                  status:
                    action === 'COMMAND_ACTION_PAUSE'
                      ? 'COMMAND_ACK_STATUS_PENDING'
                      : 'COMMAND_ACK_STATUS_CONSUMED',
                },
              })}\n`,
            );
          }
        });
      },
      async client => {
        const pause = client.request({case: 'pause', value: {}});
        const resume = client.request({case: 'resume', value: {}});
        await expect(pause).resolves.toMatchObject({ack: {action: CommandAction.PAUSE}});
        await expect(resume).resolves.toMatchObject({ack: {action: CommandAction.RESUME}});
      },
    );
  });

  it('rejects structured backend errors', async () => {
    await withServer(
      socket =>
        respondToLines(socket, request => {
          socket.write(
            `${JSON.stringify({
              protocol_version: 2,
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
                scope: 'DIAGNOSTIC_SCOPE_REQUEST',
                severity: 'DIAGNOSTIC_SEVERITY_ERROR',
                retryability: 'DIAGNOSTIC_RETRYABILITY_MANUAL',
              },
            })}\n`,
          );
        }),
      async client => {
        const rejected = client.request({case: 'snapshot', value: {}});
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
        await expect(client.request({case: 'snapshot', value: {}})).rejects.toThrow(
          'Server disconnected',
        );
      },
    );
  });

  it('rejects malformed responses instead of throwing from the socket callback', async () => {
    await withServer(
      socket => socket.once('data', () => socket.write('{not-json}\n')),
      async client => {
        await expect(client.request({case: 'snapshot', value: {}})).rejects.toThrow(
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
            `${JSON.stringify({...successResponse(request['request_id'] as string), protocol_version: 1})}\n`,
          );
        }),
      async client => {
        await expect(client.request({case: 'snapshot', value: {}})).rejects.toThrow(
          'Unsupported server protocol version',
        );
      },
    );
  });

  it('sends proto field names and the current protocol version', async () => {
    const frames: Array<Record<string, unknown>> = [];
    await withServer(
      socket =>
        respondToLines(socket, request => {
          frames.push(request);
          socket.write(`${JSON.stringify(successResponse(request['request_id'] as string))}\n`);
        }),
      async client => {
        await client.request({case: 'steer', value: {text: 'go left'}});
        expect(frames[0]).toMatchObject({protocol_version: 2, steer: {text: 'go left'}});
        expect(typeof frames[0]?.['request_id']).toBe('string');
        expect(typeof frames[0]?.['timestamp']).toBe('string');
      },
    );
  });

  it('refuses requests that violate server bounds before sending', async () => {
    await withServer(
      socket => socket.on('data', () => undefined),
      async client => {
        await expect(client.request({case: 'steer', value: {text: ''}})).rejects.toThrow(
          'steer text is empty',
        );
        await expect(client.request({case: 'events', value: {beforeSequence: 0}})).rejects.toThrow(
          'beforeSequence must be at least 1',
        );
        await expect(client.request({case: 'events', value: {timeoutMs: 30_001}})).rejects.toThrow(
          'timeoutMs must be at most 30000',
        );
        await expect(
          client.subscribe(0, () => undefined, noopDisconnect, {tail: 0}),
        ).rejects.toThrow('tail must be at least 1');
      },
    );
  });

  it('surfaces an unsupported protocol version on a request as a ServerError', async () => {
    await withServer(
      socket =>
        respondToLines(socket, () => {
          socket.write(
            `${JSON.stringify({
              protocol_error: {
                code: 'protocol_version_unsupported',
                message: 'This server speaks protocol version 3',
              },
            })}\n`,
          );
        }),
      async client => {
        const rejected = client.request({case: 'snapshot', value: {}});
        await expect(rejected).rejects.toBeInstanceOf(ServerError);
        await expect(rejected).rejects.toMatchObject({
          code: 'protocol_version_unsupported',
          message: expect.stringContaining('protocol version'),
        });
      },
    );
  });

  it('surfaces an unsupported protocol version on subscribe as a ServerError', async () => {
    await withServer(
      socket =>
        respondToLines(socket, () => {
          socket.write(
            `${JSON.stringify({
              protocol_error: {
                code: 'protocol_version_unsupported',
                message: 'This server speaks protocol version 3',
              },
            })}\n`,
          );
        }),
      async client => {
        await expect(client.subscribe(0, () => undefined, noopDisconnect)).rejects.toMatchObject({
          name: 'ServerError',
          code: 'protocol_version_unsupported',
        });
      },
    );
  });

  it('times out requests that never receive a response', async () => {
    await withServer(
      socket => socket.on('data', () => undefined),
      async client => {
        await expect(client.request({case: 'snapshot', value: {}})).rejects.toThrow(
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
          if (request['chat'] === undefined) return;
          socket.once('close', () => resolveChatSocketClosed?.());
          setTimeout(() => {
            const response = JSON.stringify({
              ...successResponse(request['request_id'] as string),
              chat: {
                question: 'what happened?',
                answer: 'The agent finished its investigation.',
              },
            });
            const middle = Math.floor(response.length / 2);
            socket.write(response.slice(0, middle));
            socket.write(`${response.slice(middle)}\n`);
          }, 50);
        });
      },
      async client => {
        const chat = client.request({case: 'chat', value: {text: 'what happened?'}});
        await expect(client.request({case: 'snapshot', value: {}})).rejects.toThrow(
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
          if (request['chat'] !== undefined) socket.write('{not-json}\n');
        }),
      async client => {
        await expect(
          client.request({case: 'chat', value: {text: 'what happened?'}}),
        ).rejects.toThrow('Invalid server response JSON');
      },
    );
  });

  it('reassembles and validates fragmented subscription messages', async () => {
    await withServer(
      socket =>
        respondToLines(socket, request => {
          if (request['subscribe'] === undefined) return;
          const subscribed = `${JSON.stringify({
            subscribed: {
              request_id: request['request_id'],
              run_id: 'run-1',
              latest_sequence: 1,
            },
          })}\n`;
          const batch = `${JSON.stringify({
            event_batch: {
              events: [
                {
                  protocol_version: 2,
                  sequence: 1,
                  timestamp: new Date().toISOString(),
                  type: 'EVENT_TYPE_SERVER_STARTED',
                },
              ],
            },
          })}\n`;
          socket.write(subscribed.slice(0, 10));
          socket.write(`${subscribed.slice(10)}${batch}`);
        }),
      async client => {
        const messages: string[] = [];
        const subscription = await client.subscribe(
          0,
          message => messages.push(String(message.body.case)),
          error => {
            throw error;
          },
        );
        expect(messages).toEqual(['subscribed', 'eventBatch']);
        await subscription.close();
      },
    );
  });

  it('carries a subscribe tail only when one is asked for', async () => {
    const frames: Array<Record<string, unknown>> = [];
    await withServer(
      socket =>
        respondToLines(socket, request => {
          if (request['subscribe'] === undefined) return;
          frames.push(request);
          socket.write(
            `${JSON.stringify({
              subscribed: {
                request_id: request['request_id'],
                run_id: 'run-1',
                latest_sequence: 0,
              },
            })}\n`,
          );
        }),
      async client => {
        const tailed = await client.subscribe(0, () => undefined, noopDisconnect, {tail: 1000});
        const full = await client.subscribe(0, () => undefined, noopDisconnect);

        expect(frames[0]?.['subscribe']).toMatchObject({tail: 1000});
        // An old server forbids unknown fields, so the plain call must not
        // carry the key at all, not even as null.
        expect(frames[1]?.['subscribe']).not.toHaveProperty('tail');
        await tailed.close();
        await full.close();
      },
    );
  });

  it('reports an event-stream disconnect only once', async () => {
    await withServer(
      socket =>
        respondToLines(socket, request => {
          if (request['subscribe'] === undefined) return;
          socket.write(
            `${JSON.stringify({
              subscribed: {
                request_id: request['request_id'],
                run_id: 'run-1',
                latest_sequence: 0,
              },
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
          if (request['subscribe'] === undefined) return;
          socket.write(
            `${JSON.stringify({
              subscribed: {
                request_id: request['request_id'],
                run_id: 'run-1',
                latest_sequence: 0,
              },
            })}\n${JSON.stringify({
              protocol_error: {
                code: 'stream_failed',
                message: 'Event stream failed',
                diagnostic: {
                  id: 'stream-1',
                  code: 'stream_failed',
                  summary: 'Event stream failed',
                  detail: 'RuntimeError: event store is unavailable',
                  scope: 'DIAGNOSTIC_SCOPE_PROTOCOL',
                  severity: 'DIAGNOSTIC_SEVERITY_ERROR',
                },
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
          message => messages.push(String(message.body.case)),
          error => disconnects.push(error),
        );
        await new Promise(resolve => setTimeout(resolve, 20));

        expect(messages).toEqual(['subscribed', 'protocolError']);
        expect(disconnects).toEqual([]);
      },
    );
  });

  it('reports unknown event-stream message fields as protocol errors', async () => {
    await withServer(
      socket =>
        respondToLines(socket, request => {
          if (request['subscribe'] === undefined) return;
          socket.write(
            `${JSON.stringify({
              subscribed: {
                request_id: request['request_id'],
                run_id: 'run-1',
                latest_sequence: 0,
              },
            })}\n${JSON.stringify({unknown: {}})}\n`,
          );
        }),
      async client => {
        const disconnect = new Promise<Error>(resolve => {
          void client.subscribe(0, () => undefined, resolve);
        });
        await expect(disconnect).resolves.toMatchObject({
          message: expect.stringContaining('is unknown'),
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
      await expect(client.request({case: 'snapshot', value: {}})).resolves.toMatchObject({
        ok: true,
      });
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
    protocol_version: 2,
    request_id: requestId,
    timestamp: new Date().toISOString(),
    ok: true,
    snapshot: {
      protocol_version: 2,
      run_id: 'test',
      sequence: 1,
      status: 'RUN_STATUS_RUNNING',
    },
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
