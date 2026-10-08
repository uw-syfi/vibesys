import {expect, test} from 'bun:test';
import {loadReplayFixture} from './replay.js';
import {createCoreStateStore} from './store.js';

test('loads JSONL events into the shared store', async () => {
  const store = createCoreStateStore();

  await loadReplayFixture(
    store,
    undefined,
    undefined,
    async () =>
      new Response('{"type":"server_ready","sequence":1,"timestamp":"2026-09-27T00:00:00Z"}\n'),
  );

  expect(store.getState().sequence).toBe(1);
});

test('reports the HTTP status without mutating the store', async () => {
  const store = createCoreStateStore();

  await expect(
    loadReplayFixture(
      store,
      undefined,
      undefined,
      async () => new Response('unavailable', {status: 503}),
    ),
  ).rejects.toThrow('Replay fixture request failed with 503');
  expect(store.getState().sequence).toBe(0);
});

test('reports the line containing malformed replay JSON', async () => {
  const store = createCoreStateStore();

  await expect(
    loadReplayFixture(
      store,
      undefined,
      undefined,
      async () =>
        new Response(
          '{"type":"server_ready","sequence":1,"timestamp":"2026-09-27T00:00:00Z"}\n{\n',
        ),
    ),
  ).rejects.toThrow('Replay fixture contains invalid JSON on line 2');
  expect(store.getState().sequence).toBe(0);
});

test('rejects one malformed event atomically and names its line and field', async () => {
  const store = createCoreStateStore();

  await expect(
    loadReplayFixture(
      store,
      undefined,
      undefined,
      async () =>
        new Response(
          '{"type":"server_ready","sequence":1,"timestamp":"2026-09-27T00:00:00Z"}\n' +
            '{"type":"server_ready","sequence":2}\n',
        ),
    ),
  ).rejects.toThrow(
    'Replay fixture contains invalid event on line 2: Invalid server run event: timestamp must be a string',
  );
  expect(store.getState().sequence).toBe(0);
});

test('rejects malformed fields inside known event data before folding any line', async () => {
  const store = createCoreStateStore();

  await expect(
    loadReplayFixture(
      store,
      undefined,
      undefined,
      async () =>
        new Response(
          '{"type":"server_ready","sequence":1,"timestamp":"2026-09-27T00:00:00Z"}\n' +
            '{"type":"agent_execution_started","sequence":2,"timestamp":"2026-09-27T00:00:01Z","data":{"kind":"agent_execution_started","stage":"implement"}}\n',
        ),
    ),
  ).rejects.toThrow(
    'Replay fixture contains invalid event on line 2: Invalid server run event.data: activity must be present',
  );
  expect(store.getState().sequence).toBe(0);
});

test('accepts unknown closed-set members with valid wire kinds', async () => {
  const store = createCoreStateStore();

  await loadReplayFixture(
    store,
    undefined,
    undefined,
    async () =>
      new Response(
        JSON.stringify({
          protocol_version: 1,
          sequence: 1,
          timestamp: '2026-09-27T00:00:00Z',
          type: 'future-event-type',
          status: 'future-status',
          data: {kind: 'future-data-kind', future_field: true},
        }),
      ),
  );

  expect(store.getState().sequence).toBe(1);
});

test('rejects an incompatible event protocol version', async () => {
  const store = createCoreStateStore();

  await expect(
    loadReplayFixture(
      store,
      undefined,
      undefined,
      async () =>
        new Response(
          '{"protocol_version":2,"type":"server_ready","sequence":1,"timestamp":"2026-09-27T00:00:00Z"}\n',
        ),
    ),
  ).rejects.toThrow(
    'Replay fixture contains invalid event on line 1: Invalid server run event: protocol_version must be 1',
  );
  expect(store.getState().sequence).toBe(0);
});
