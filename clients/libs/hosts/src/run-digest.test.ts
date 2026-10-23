import {describe, expect, test} from 'bun:test';
import {digestLook, type RunDigestEntry} from './run-digest.js';

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

describe('digestLook', () => {
  test('reads the ending record first and the terminal event after it', () => {
    expect(digestLook(read({outcome: 'stopped', closing: 'finished'}))?.kind).toBe('stopped');
    expect(digestLook(read({closing: 'finished'}))?.tone).toBe('ok');
    expect(digestLook(read({closing: 'failed'}))?.tone).toBe('err');
    expect(digestLook(read({closing: 'interrupted'}))?.kind).toBe('stopped');
    expect(digestLook(read({}))).toBeNull();
  });
});
