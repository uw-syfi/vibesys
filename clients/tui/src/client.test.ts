import {createServer} from 'node:net';
import {unlink} from 'node:fs/promises';
import {join} from 'node:path';
import {randomUUID} from 'node:crypto';
import {afterEach, describe, expect, it} from 'vitest';
import {SupervisionClient} from './client.js';

let socketPath: string | undefined;

afterEach(async () => {
  if (socketPath) await unlink(socketPath).catch(() => undefined);
  socketPath = undefined;
});

describe('SupervisionClient', () => {
  it('correlates JSONL responses with requests', async () => {
    socketPath = join('/tmp', `vs-${randomUUID().slice(0, 8)}.sock`);
    const server = createServer(socket => {
      let buffer = '';
      socket.setEncoding('utf8');
      socket.on('data', chunk => {
        buffer += chunk;
        const newline = buffer.indexOf('\n');
        if (newline < 0) return;
        const request = JSON.parse(buffer.slice(0, newline));
        socket.write(`${JSON.stringify({
          protocol_version: 1,
          request_id: request.request_id,
          timestamp: new Date().toISOString(),
          ok: true,
          snapshot: {
            protocol_version: 1,
            run_id: 'test',
            sequence: 1,
            status: 'running',
          },
          events: [],
        })}\n`);
      });
    });
    await new Promise<void>(resolve => server.listen(socketPath, resolve));

    const client = await SupervisionClient.connect(socketPath);
    const response = await client.request({type: 'query.status'});

    expect(response.snapshot?.status).toBe('running');
    await client.close();
    await new Promise<void>((resolve, reject) =>
      server.close(error => error ? reject(error) : resolve()),
    );
  });
});
