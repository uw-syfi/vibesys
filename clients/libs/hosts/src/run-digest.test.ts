import {describe, expect, test} from 'bun:test';
import type {RunSummary} from '@vibesys/contracts/host-helper';
import {digestEntries, digestLook, type RunDigestEntry} from './run-digest.js';

const summary = (runId: string, digest?: RunSummary['digest']): RunSummary => ({
  run_id: runId,
  task: null,
  display_name: runId,
  created_at: '2026-10-17T03:00:00Z',
  phase: null,
  instance_id: null,
  ...(digest === undefined ? {} : {digest}),
});

type Read = Extract<RunDigestEntry, {kind: 'read'}>;
const read = (over: Partial<Read>): Read => ({
  kind: 'read',
  createdAt: 0,
  ideas: 0,
  bestRatio: null,
  durationSeconds: null,
  outcome: null,
  closing: null,
  over: false,
  ...over,
});

describe('digestEntries', () => {
  test('keeps the runs that carry a digest, reads and unreadable alike, and drops the others', () => {
    const entries = digestEntries([
      summary('a', {
        kind: 'digest',
        ideas: 2,
        best_ratio: 1.5,
        duration_s: 10,
        ending: {outcome: 'adopted', summary: '', resumable: false, rounds_finished: 2},
        closing: null,
      }),
      summary('b', {kind: 'unreadable', message: 'could not read'}),
      summary('c'),
    ]);
    expect([...entries.keys()]).toEqual(['a', 'b']);
    expect(entries.get('a')).toMatchObject({kind: 'read', outcome: 'adopted', over: true});
    expect(entries.get('b')).toMatchObject({kind: 'unreadable', message: 'could not read'});
  });

  test('a journal with neither an ending nor a terminal event is not over', () => {
    const entries = digestEntries([
      summary('a', {
        kind: 'digest',
        ideas: 0,
        best_ratio: null,
        duration_s: null,
        ending: null,
        closing: null,
      }),
    ]);
    expect(entries.get('a')).toMatchObject({kind: 'read', over: false});
  });
});

describe('digestLook', () => {
  test('reads the ending record first and the terminal event after it', () => {
    expect(digestLook(read({outcome: 'stopped', closing: 'finished'}))?.kind).toBe('stopped');
    expect(digestLook(read({closing: 'finished'}))?.tone).toBe('ok');
    expect(digestLook(read({closing: 'failed'}))?.tone).toBe('err');
    expect(digestLook(read({closing: 'interrupted'}))?.kind).toBe('stopped');
    expect(digestLook(read({}))).toBeNull();
  });
});
