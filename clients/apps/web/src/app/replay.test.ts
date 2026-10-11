import {expect, test} from 'bun:test';
import {initialCoreState} from '@vibesys/core-state';
import {createReplayStore} from '@vibesys/feature-replay/renderer';
import {runStep} from '@vibesys/feature-run-view/state';
import {fetchReplayFixture} from '../platform/backend.js';

/** Load the replay demo over `fetcher`; resolves once the load settles. */
async function replay(fetcher: () => Promise<Response>) {
  let run = initialCoreState();
  const store = createReplayStore(
    events => {
      run = runStep(run, {type: 'events', events}).state;
    },
    url => fetchReplayFixture(url, fetcher),
  );
  const settled = new Promise<void>(resolve => {
    store.subscribe(() => {
      if (store.getState().kind !== 'loading') resolve();
    });
  });
  store.dispatch({type: 'load'});
  await settled;
  const state = store.getState();
  return {sequence: run.sequence, failure: state.kind === 'failed' ? state.error : null};
}

test('loads JSONL events into the shared store', async () => {
  const {sequence, failure} = await replay(
    async () =>
      new Response('{"type":"server_ready","sequence":1,"timestamp":"2026-09-27T00:00:00Z"}\n'),
  );
  expect(failure).toBeNull();

  expect(sequence).toBe(1);
});

test('reports the HTTP status without mutating the store', async () => {
  const {sequence, failure} = await replay(async () => new Response('unavailable', {status: 503}));
  expect(failure).toBe('Replay fixture request failed with 503');
  expect(sequence).toBe(0);
});

test('reports the line containing malformed replay JSON', async () => {
  const {sequence, failure} = await replay(
    async () =>
      new Response('{"type":"server_ready","sequence":1,"timestamp":"2026-09-27T00:00:00Z"}\n{\n'),
  );
  expect(failure).toBe('Replay fixture contains invalid JSON on line 2');
  expect(sequence).toBe(0);
});

test('rejects one malformed event atomically and names its line and field', async () => {
  const {sequence, failure} = await replay(
    async () =>
      new Response(
        '{"type":"server_ready","sequence":1,"timestamp":"2026-09-27T00:00:00Z"}\n' +
          '{"type":"server_ready","sequence":2}\n',
      ),
  );
  expect(failure).toBe(
    'Replay fixture contains invalid event on line 2: Invalid server run event: timestamp must be a string',
  );
  expect(sequence).toBe(0);
});

test('rejects malformed fields inside known event data before folding any line', async () => {
  const {sequence, failure} = await replay(
    async () =>
      new Response(
        '{"type":"server_ready","sequence":1,"timestamp":"2026-09-27T00:00:00Z"}\n' +
          '{"type":"agent_execution_started","sequence":2,"timestamp":"2026-09-27T00:00:01Z","data":{"kind":"agent_execution_started","stage":"implement"}}\n',
      ),
  );
  expect(failure).toBe(
    'Replay fixture contains invalid event on line 2: Invalid server run event.data: activity must be present',
  );
  expect(sequence).toBe(0);
});

test('accepts unknown closed-set members with valid wire kinds', async () => {
  const {sequence, failure} = await replay(
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
  expect(failure).toBeNull();

  expect(sequence).toBe(1);
});

test('rejects an incompatible event protocol version', async () => {
  const {sequence, failure} = await replay(
    async () =>
      new Response(
        '{"protocol_version":2,"type":"server_ready","sequence":1,"timestamp":"2026-09-27T00:00:00Z"}\n',
      ),
  );
  expect(failure).toBe(
    'Replay fixture contains invalid event on line 1: Invalid server run event: protocol_version must be 1',
  );
  expect(sequence).toBe(0);
});
