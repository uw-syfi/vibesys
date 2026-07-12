import {randomUUID} from 'node:crypto';
import {unlink} from 'node:fs/promises';
import {createServer, type Server, type Socket} from 'node:net';
import {join} from 'node:path';
import {afterEach, describe, expect, it} from 'vitest';
import {SupervisionClient} from './client.js';

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
});

async function withServer(
  onConnection: (socket: Socket) => void,
  test: (client: SupervisionClient) => Promise<void>,
): Promise<void> {
  socketPath = join('/tmp', `vs-${randomUUID().slice(0, 8)}.sock`);
  const server = createServer(onConnection);
  await listen(server, socketPath);
  const client = await SupervisionClient.connect(socketPath);
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
