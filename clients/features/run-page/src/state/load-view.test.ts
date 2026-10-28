import {expect, test} from 'bun:test';
import type {SkeletonLoadFailure} from '@vibesys/run-skeleton';
import {loadView} from './index.js';

const FAILURES: readonly [SkeletonLoadFailure, string, string][] = [
  [
    {kind: 'refused', reason: 'not-found', cause: new Error('run.log is missing')},
    'No run log',
    'run.log is missing',
  ],
  [{kind: 'refused', reason: 'denied', cause: 'no access'}, 'Denied', 'no access'],
  [{kind: 'data', cause: new Error('bad record 7')}, 'Invalid records', 'bad record 7'],
  [{kind: 'transport', cause: new Error('boom')}, 'Unreachable', 'boom'],
  [{kind: 'deadline', operation: 'skeleton'}, 'Timed out', ''],
  [{kind: 'empty'}, 'No records', ''],
  [{kind: 'stream-ended'}, 'Unreachable', ''],
];

test('a failed load keeps its status word and carries what the failure said, for copying', () => {
  for (const [failure, word, detail] of FAILURES)
    expect(loadView({progress: {phase: 'skeleton'}, failure, terminal: true})).toEqual({
      kind: 'failed',
      word,
      detail,
    });
});
