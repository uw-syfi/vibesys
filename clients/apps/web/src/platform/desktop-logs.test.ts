import {describe, expect, test} from 'bun:test';
import type {RunLogsRequest} from '@vibesys/ipc';
import type {LogListOutcome} from '@vibesys/logs';
import {desktopLogSource} from './desktop-logs.js';

const LISTED: LogListOutcome = {ok: true, instanceId: '0123456789ab', streams: []};
const READ = {
  instance_id: '0123456789ab',
  stream_id: '000001',
  start: {tail_bytes: 4096},
  max_bytes: 4096,
  wait_ms: 0,
};

function shell(logs: (request: RunLogsRequest) => Promise<unknown>) {
  const asked: RunLogsRequest[] = [];
  const bridge = {
    platform: 'darwin',
    logs: (request: RunLogsRequest) => {
      asked.push(request);
      return logs(request);
    },
  };
  const source = desktopLogSource(bridge);
  if (source === null) throw new Error('the desktop shell has no log source');
  return {source, asked};
}

const NEVER = new AbortController().signal;

describe('desktopLogSource', () => {
  test('a browser, or a shell without logs, has no source', () => {
    expect(desktopLogSource(undefined)).toBeNull();
    expect(desktopLogSource({platform: 'darwin'})).toBeNull();
  });

  test("passes the page's requests to the shell and its answers back", async () => {
    const {source, asked} = shell(async () => LISTED);
    expect(await source.list(NEVER)).toEqual(LISTED);
    await source.read(READ, NEVER);
    expect(asked).toEqual([{kind: 'list'}, {kind: 'read', token: expect.any(String), read: READ}]);
  });

  test('a refused or malformed answer is a transport failure, not a rejection', async () => {
    const refused = shell(async () => {
      throw new Error('this view is not bound to a run');
    });
    expect(await refused.source.list(NEVER)).toEqual({
      ok: false,
      failure: {code: 'transport', message: 'this view is not bound to a run'},
    });
    const malformed = shell(async () => 'yes');
    expect(await malformed.source.read(READ, NEVER)).toMatchObject({
      ok: false,
      failure: {code: 'transport'},
    });
  });

  test('an aborted request rejects with its reason and drops the late answer', async () => {
    let answer: (value: unknown) => void = () => {};
    const {source} = shell(() => new Promise(resolve => (answer = resolve)));
    const abort = new AbortController();
    const reading = source.read(READ, abort.signal);
    abort.abort(new Error('the panel collapsed'));
    answer({ok: true, chunk: {}});
    await expect(reading).rejects.toThrow('the panel collapsed');
  });

  test('aborting a read withdraws it by its token; an answered read sends none', async () => {
    const answers: ((value: unknown) => void)[] = [];
    const {source, asked} = shell(request =>
      request.kind === 'withdraw'
        ? Promise.reject(new Error('the window closed'))
        : new Promise(resolve => answers.push(resolve)),
    );
    const reads = 4;
    const aborts = Array.from({length: reads}, () => new AbortController());
    const reading = aborts.map(abort => source.read(READ, abort.signal));
    answers[0]?.({ok: true, chunk: {}});
    expect(await reading[0]).toMatchObject({ok: true});
    for (const abort of aborts) abort.abort(new Error('the panel collapsed'));
    for (const read of reading.slice(1)) await expect(read).rejects.toThrow('the panel collapsed');

    const tokens = asked.flatMap(request => (request.kind === 'read' ? [request.token] : []));
    expect(new Set(tokens).size).toBe(reads);
    const withdrawn = asked.flatMap(request =>
      request.kind === 'withdraw' ? [request.token] : [],
    );
    expect(withdrawn).toEqual(tokens.slice(1));
  });

  test('a read aborted before it was asked sends neither it nor a withdrawal', async () => {
    const {source, asked} = shell(async () => LISTED);
    const abort = new AbortController();
    abort.abort(new Error('gone'));
    await expect(source.read(READ, abort.signal)).rejects.toThrow('gone');
    expect(asked).toEqual([]);
  });
});
