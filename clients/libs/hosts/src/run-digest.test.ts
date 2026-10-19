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
  adoptedRatio: null,
  bestSoFarRatio: null,
  durationSeconds: null,
  outcome: null,
  closing: null,
  over: false,
  dashboard: null,
  ...over,
});

describe('digestEntries', () => {
  test('keeps the runs that carry a digest, reads and unreadable alike, and drops the others', () => {
    const entries = digestEntries([
      summary('a', {
        kind: 'digest',
        ideas: 2,
        adopted_ratio: 1.5,
        best_so_far_ratio: null,
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

  test('keeps the account of how the run ended, and none when it has none', () => {
    const digest = (summary: string) =>
      ({
        kind: 'digest',
        ideas: 1,
        adopted_ratio: null,
        best_so_far_ratio: null,
        duration_s: 5,
        ending: {outcome: null, summary, resumable: false, rounds_finished: 0},
        closing: null,
      }) as const;
    const entries = digestEntries([
      summary('a', digest('the agent crashed')),
      summary('b', digest(' ')),
    ]);
    expect(entries.get('a')).toMatchObject({kind: 'read', endSummary: 'the agent crashed'});
    expect(entries.get('b')).toMatchObject({kind: 'read', endSummary: null});
  });

  test('a digest that counts no ideas because the journal predates the trace keeps that as null', () => {
    const entries = digestEntries([
      summary('a', {
        kind: 'digest',
        ideas: null,
        adopted_ratio: null,
        best_so_far_ratio: null,
        duration_s: 5,
        ending: null,
        closing: 'finished',
      }),
    ]);
    expect(entries.get('a')).toMatchObject({kind: 'read', ideas: null, over: true});
  });

  test('a journal with neither an ending nor a terminal event is not over', () => {
    const entries = digestEntries([
      summary('a', {
        kind: 'digest',
        ideas: 0,
        adopted_ratio: null,
        best_so_far_ratio: null,
        duration_s: null,
        ending: null,
        closing: null,
      }),
    ]);
    expect(entries.get('a')).toMatchObject({kind: 'read', over: false});
  });
});

describe('the best reading of a tile', () => {
  const withDashboard = (best?: {value: number; metric: string | null; unit: string | null}) =>
    summary('a', {
      kind: 'digest',
      ideas: 1,
      adopted_ratio: null,
      best_so_far_ratio: null,
      duration_s: 5,
      ending: null,
      closing: null,
      dashboard: {
        points: [],
        points_omitted: 0,
        failed_streak: 0,
        last_event_at: null,
        ...(best === undefined ? {} : {best}),
      },
    });

  test('is carried as the host measured it, and is null when the host sends none', () => {
    const best = {value: 287.4, metric: 'output_tokens_per_s', unit: null};
    const entries = digestEntries([withDashboard(best)]);
    expect(entries.get('a')).toMatchObject({dashboard: {best}});
    expect(digestEntries([withDashboard()]).get('a')).toMatchObject({dashboard: {best: null}});
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
