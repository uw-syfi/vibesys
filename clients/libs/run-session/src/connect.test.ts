import {describe, expect, test} from 'bun:test';
import {type ProtocolResponse, ServerError} from '@vibesys/contracts';
import type {AbortSignalLike} from '@vibesys/transport';
import {connectRun} from './index.js';
import {FakeTransport} from './testing/index.js';

const activeSignal: AbortSignalLike = {
  aborted: false,
  addEventListener() {},
  removeEventListener() {},
};

describe('connectRun iteration capability', () => {
  test('keeps every structured first-query failure visible and retryable', async () => {
    let transport: FakeTransport | undefined;
    const connection = connectRun(hooks => {
      transport = new FakeTransport(hooks);
      return transport;
    })({onConnectionState() {}});
    if (transport === undefined) throw new Error('connectRun did not construct its transport');

    const failures = [
      'iteration_query_invalid',
      'operation_failed',
      'future_structured_failure',
    ].map(
      code =>
        new ServerError(`Structured iteration failure: ${code}`, {
          code,
          summary: `Structured iteration failure: ${code}`,
          scope: 'request',
        }),
    );
    transport.iterations.push(...failures, iterationPageResponse());

    for (const failure of failures) {
      await expect(connection.loadIterations(null, 'store-1', activeSignal)).rejects.toBe(failure);
    }
    await expect(connection.loadIterations(null, 'store-1', activeSignal)).resolves.toMatchObject({
      kind: 'result',
      result: {projection_revision: 'recovered'},
    });
    expect(transport.requests.filter(request => request.type === 'query.iterations')).toHaveLength(
      4,
    );

    await connection.close();
  });
});

function iterationPageResponse(): ProtocolResponse {
  return {
    protocol_version: 1,
    request_id: 'iterations-recovered',
    timestamp: '2026-01-01T00:00:00Z',
    ok: true,
    iterations_ready: true,
    iterations: {
      kind: 'page',
      schema_version: 1,
      run_id: 'run-1',
      projection_revision: 'recovered',
      parent_id: null,
      from_cursor: null,
      items: [],
      next_cursor: null,
    },
  };
}
