import {randomUUID} from 'node:crypto';
import {unlink} from 'node:fs/promises';
import {createServer, type Server, type Socket} from 'node:net';
import {join} from 'node:path';
import {afterEach, describe, expect, it} from 'vitest';
import {SupervisionClient, type SupervisionClientOptions} from './client.js';

let socketPath: string | undefined;

afterEach(async () => {
  if (socketPath) await unlink(socketPath).catch(() => undefined);
  socketPath = undefined;
});

describe('SupervisionClient', () => {
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
              events: [],
            })}\n`,
          );
        }),
      async client => {
        await expect(client.request({type: 'query.snapshot'})).rejects.toThrow('invalid request');
      },
    );
  });

  it('rejects pending requests when the server disconnects', async () => {
    await withServer(
      socket => socket.once('data', () => socket.destroy()),
      async client => {
        await expect(client.request({type: 'query.snapshot'})).rejects.toThrow(
          'Supervision server disconnected',
        );
      },
    );
  });

  it('rejects malformed responses instead of throwing from the socket callback', async () => {
    await withServer(
      socket => socket.once('data', () => socket.write('{not-json}\n')),
      async client => {
        await expect(client.request({type: 'query.snapshot'})).rejects.toThrow(
          'Invalid supervision response JSON',
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
          'Unsupported supervision protocol version',
        );
      },
    );
  });

  it('times out requests that never receive a response', async () => {
    await withServer(
      socket => socket.on('data', () => undefined),
      async client => {
        await expect(client.request({type: 'query.snapshot'})).rejects.toThrow(
          'Supervision request timed out after 20ms',
        );
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
          message: expect.stringContaining('Unknown supervision event-stream message'),
        });
      },
    );
  });
});

async function withServer(
  onConnection: (socket: Socket) => void,
  test: (client: SupervisionClient) => Promise<void>,
  options: SupervisionClientOptions = {},
): Promise<void> {
  socketPath = join('/tmp', `vs-${randomUUID().slice(0, 8)}.sock`);
  const server = createServer(onConnection);
  await listen(server, socketPath);
  const client = await SupervisionClient.connect(socketPath, options);
  try {
    await test(client);
  } finally {
    await client.close();
    await close(server);
  }
}

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
