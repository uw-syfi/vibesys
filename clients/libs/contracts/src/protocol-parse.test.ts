import {it} from 'node:test';
import {parseActiveExecutionsCheckpoint, parseServerMessage} from './index.js';
import {expect} from './test-support/expect.js';

function chunk(overrides: Record<string, unknown> = {}): string {
  return JSON.stringify({
    type: 'active_executions_chunk',
    checkpoint_id: 'a'.repeat(64),
    chunk_index: 0,
    chunk_count: 1,
    payload: '[]',
    through_sequence: 7,
    store_id: 'store',
    history_after_sequence: 3,
    ...overrides,
  });
}

it('accepts the generated checkpoint chunk arm and a null deferred checkpoint', () => {
  expect(parseServerMessage(chunk())).toMatchObject({
    type: 'active_executions_chunk',
    chunk_index: 0,
    chunk_count: 1,
  });
  expect(
    parseServerMessage(
      JSON.stringify({
        type: 'event_batch',
        events: [],
        active_executions: null,
        active_executions_checkpoint_id: 'a'.repeat(64),
      }),
    ),
  ).toMatchObject({type: 'event_batch', active_executions: null});
});

it('rejects checkpoint indexes, identities, and payloads outside their public bounds', () => {
  expect(() => parseServerMessage(chunk({chunk_index: 1}))).toThrow(
    'chunk_index must be smaller than chunk_count',
  );
  expect(() => parseServerMessage(chunk({checkpoint_id: 'not-a-checkpoint'}))).toThrow(
    'checkpoint_id must be 64 lowercase hexadecimal characters',
  );
  expect(() => parseServerMessage(chunk({chunk_count: 257}))).toThrow(
    'chunk_count must be an integer from 1 to 256',
  );
  expect(() => parseServerMessage(chunk({payload: 'x'.repeat(64 * 1024 + 1)}))).toThrow(
    'payload must be at most 65536 Unicode characters',
  );
  expect(() =>
    parseServerMessage(JSON.stringify({type: 'event_batch', events: [], active_executions: null})),
  ).toThrow('active_executions_checkpoint_id must be the checkpoint identity');
  expect(() =>
    parseServerMessage(
      JSON.stringify({
        type: 'event_batch',
        events: [],
        active_executions: [],
        active_executions_checkpoint_id: 'a'.repeat(64),
      }),
    ),
  ).toThrow('active_executions_checkpoint_id must be null or absent');
  expect(() =>
    parseServerMessage(
      JSON.stringify({
        type: 'event_batch',
        events: [],
        active_executions_checkpoint_id: 'a'.repeat(64),
      }),
    ),
  ).toThrow('active_executions_checkpoint_id must be null or absent');
});

it('validates every execution after a checkpoint has been assembled', () => {
  expect(parseActiveExecutionsCheckpoint('[]')).toEqual([]);
  expect(() => parseActiveExecutionsCheckpoint('[{"execution_id":"only-one-field"}]')).toThrow(
    'active-execution checkpoint[0]: agent_kind must be a string',
  );
});
