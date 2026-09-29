import {strict as assert} from 'node:assert';
import {test} from 'node:test';
import {
  asDraft,
  httpNotesApi,
  INITIAL_NOTE,
  type NoteState,
  type NotesApi,
  noteReducer,
  serialSaver,
} from './notes.js';

const RECORD = {
  runId: 'a/b c',
  text: 'check p99',
  createdAt: '2026-09-25T14:00:00Z',
  updatedAt: '2026-09-25T14:05:00Z',
};

test('the notes client: bearer token, encoded run id, JSON body, keepalive on request', async () => {
  const calls: {url: string; init: RequestInit | undefined}[] = [];
  const fetcher = (async (url: string, init?: RequestInit) => {
    calls.push({url, init});
    return new Response(JSON.stringify({note: RECORD}), {status: 200});
  }) as typeof fetch;
  const api = httpNotesApi('tok', fetcher);
  assert.deepEqual(await api.get('a/b c'), RECORD);
  await api.put('a/b c', 'check p99', true);
  assert.equal(calls[0]?.url, '/api/notes/a%2Fb%20c');
  assert.deepEqual(calls[0]?.init?.headers, {Authorization: 'Bearer tok'});
  assert.equal(calls[1]?.init?.method, 'PUT');
  assert.equal(calls[1]?.init?.body, JSON.stringify({text: 'check p99'}));
  assert.equal(calls[1]?.init?.keepalive, true);
  assert.deepEqual(calls[1]?.init?.headers, {
    Authorization: 'Bearer tok',
    'Content-Type': 'application/json',
  });
});

test('the notes client reports the server message, or the status when the body is not JSON', async () => {
  const typed = httpNotesApi(
    'tok',
    (async (_url: string, _init?: RequestInit) =>
      new Response(JSON.stringify({error: {code: 'invalid_request', message: 'run id too long'}}), {
        status: 400,
      })) as typeof fetch,
  );
  await assert.rejects(typed.get('x'), /run id too long/);
  const bare = httpNotesApi(
    'tok',
    (async (_url: string, _init?: RequestInit) =>
      new Response('Not found', {status: 404})) as typeof fetch,
  );
  await assert.rejects(bare.get('x'), /Notes are unavailable \(HTTP 404\)/);
  const empty = httpNotesApi(
    'tok',
    (async (_url: string, _init?: RequestInit) =>
      new Response(JSON.stringify({note: null}), {status: 200})) as typeof fetch,
  );
  assert.equal(await empty.get('x'), null);
});

test('a note loads, edits and saves; unsaved text is whatever differs from the last save', () => {
  let state = noteReducer(INITIAL_NOTE, {type: 'load', runId: 'r1'});
  state = noteReducer(state, {type: 'loaded', runId: 'r1', text: 'old'});
  state = noteReducer(state, {type: 'edit', text: 'new'});
  assert.deepEqual(state, {phase: 'ready', runId: 'r1', text: 'new', saved: 'old', error: null});
  state = noteReducer(state, {type: 'saveFailed', runId: 'r1', message: 'offline'});
  assert.equal(state.phase === 'ready' && state.error, 'offline');
  state = noteReducer(state, {type: 'saved', runId: 'r1', text: 'new'});
  assert.deepEqual(state, {phase: 'ready', runId: 'r1', text: 'new', saved: 'new', error: null});
});

test("a stale run's results are ignored", () => {
  const loading = noteReducer(INITIAL_NOTE, {type: 'load', runId: 'r2'});
  assert.equal(noteReducer(loading, {type: 'loaded', runId: 'r1', text: 'other run'}), loading);
  const ready: NoteState = {phase: 'ready', runId: 'r2', text: 'b', saved: 'a', error: null};
  assert.equal(noteReducer(ready, {type: 'saved', runId: 'r1', text: 'b'}), ready);
  assert.equal(noteReducer(ready, {type: 'saveFailed', runId: 'r1', message: 'x'}), ready);
});

test('nothing is editable before the note loads, or after it failed to load', () => {
  const loading = noteReducer(INITIAL_NOTE, {type: 'load', runId: 'r1'});
  assert.equal(noteReducer(loading, {type: 'edit', text: 'typed'}), loading);
  const failed = noteReducer(loading, {type: 'loadFailed', runId: 'r1', message: 'HTTP 500'});
  assert.deepEqual(failed, {phase: 'failed', runId: 'r1', message: 'HTTP 500'});
  assert.equal(noteReducer(failed, {type: 'edit', text: 'typed'}), failed);
});

test('saves go out one at a time, in the order they were made, past a failed one', async () => {
  const order: string[] = [];
  let release: () => void = () => {};
  const api: NotesApi = {
    get: async () => null,
    put: async (runId, text) => {
      if (text === 'first')
        await new Promise<void>(resolve => {
          release = resolve;
        });
      if (text === 'bad') throw new Error('offline');
      order.push(text);
      return {...RECORD, runId, text};
    },
  };
  const save = serialSaver(api);
  const first = save('r', 'first');
  const bad = save('r', 'bad');
  const last = save('r', 'last');
  await Promise.resolve();
  assert.deepEqual(order, []);
  release();
  await first;
  await assert.rejects(bad, /offline/);
  await last;
  assert.deepEqual(order, ['first', 'last']);
});

test('a note becomes a single-line draft', () => {
  assert.equal(asDraft('  Line one\n\n  line two \n'), 'Line one line two');
});
