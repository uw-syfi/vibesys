import {expect, test} from 'bun:test';
import {create, fromBinary, toBinary} from '@bufbuild/protobuf';
import type {HostId} from '@vibesys/hosts';
import {ReadRequestSchema, ReadResponseSchema, SavedRunImport} from '@vibesys/proto';
import {registrySavedRuns} from './registry-saved-runs.js';

const HOST: HostId = {kind: 'local'};

class Catalog {
  readonly offsets: number[] = [];
  changedRevision = false;
  stalledCursor = false;
  async checkout() {
    return '/checkout';
  }
  async unary(_host: HostId, bytes: Uint8Array, signal: AbortSignal): Promise<Uint8Array> {
    if (signal.aborted) throw new Error('cancelled');
    const envelope = fromBinary(ReadRequestSchema, bytes);
    if (envelope.method.case !== 'listSavedRuns') throw new Error('unexpected method');
    const request = envelope.method.value;
    this.offsets.push(request.offset);
    if (request.offset > 0 && request.revision !== 'a'.repeat(64))
      throw new Error('revision required');
    return toBinary(
      ReadResponseSchema,
      create(ReadResponseSchema, {
        result: {
          case: 'listSavedRuns',
          value: {
            requestId: request.requestId,
            projectRoot: '/project',
            revision: this.changedRevision && request.offset > 0 ? 'b'.repeat(64) : 'a'.repeat(64),
            nextOffset: this.stalledCursor ? 0 : request.offset === 0 ? 1 : undefined,
            runs: [
              {
                runId: `run-${request.offset}`,
                displayName: `Saved ${request.offset}`,
                task: request.offset === 0 ? 'task-a' : undefined,
                createdAt: {seconds: 100n + BigInt(request.offset)},
                imported:
                  request.offset === 0 ? SavedRunImport.AVAILABLE : SavedRunImport.UNAVAILABLE,
                detail: request.offset === 0 ? '' : 'Retained history unavailable',
              },
            ],
          },
        },
      }),
    );
  }
}

// Catalog is the boundary's in-memory implementation; the registry still encodes,
// validates and correlates every request and response exactly as in production.
function reads(catalog: Catalog) {
  return registrySavedRuns({
    checkout: () => catalog.checkout(),
    unary: (host, bytes, signal) => catalog.unary(host, bytes, signal),
  });
}

test('discovery preserves metadata, page revisions and import refusal reasons', async () => {
  const catalog = new Catalog();
  expect(await reads(catalog).list(HOST, '/project', new AbortController().signal)).toEqual([
    {
      runId: 'run-1',
      name: 'Saved 1',
      task: null,
      createdAt: 101000,
      unavailable: 'Retained history unavailable',
    },
    {runId: 'run-0', name: 'Saved 0', task: 'task-a', createdAt: 100000},
  ]);
  expect(catalog.offsets).toEqual([0, 1]);
});

test('discovery refuses catalog drift, stalled cursors and cancellation', async () => {
  for (const fault of ['changedRevision', 'stalledCursor'] as const) {
    const catalog = new Catalog();
    catalog[fault] = true;
    await expect(
      reads(catalog).list(HOST, '/project', new AbortController().signal),
    ).rejects.toThrow(fault === 'changedRevision' ? 'revision' : 'cursor');
  }
  const catalog = new Catalog();
  const controller = new AbortController();
  controller.abort();
  await expect(reads(catalog).list(HOST, '/project', controller.signal)).rejects.toThrow(
    'cancelled',
  );
  expect(catalog.offsets).toEqual([]);
});
