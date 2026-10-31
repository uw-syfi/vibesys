/**
 * Which publications of a catching-up read the page may hold back: those that differ from the one
 * before only by what the loader's batches bring. Input space: every field of the machine's
 * context, and of its screen state, changed alone; progress of any phase; a failure or none.
 */
import {expect, test} from 'bun:test';
import fc from 'fast-check';
import {getInitialSnapshot} from 'xstate';
import {type GatewayRunContext, gatewayRunMachine, isCatchUpBatch} from './index.js';

const SEED = {
  identity: {host: 'local' as const, runId: 'run-a'},
  title: {project: 'queue-rs', task: null},
  hostLabel: 'Local',
  unavailable: {},
};
const HISTORY = {phase: 'history', through: 1n, committed: 5n} as const;
const BATCH_FIELDS = ['load', 'progress', 'replies'];
const BATCH_UI_FIELDS = ['recordLog', 'recordLogError'];

const initial: GatewayRunContext = {
  ...getInitialSnapshot(gatewayRunMachine, {seed: SEED}).context,
  progress: HISTORY,
};
// A new value for a field is a new object: the predicate asks only whether a field was replaced.
const replaced = () => ({replaced: true});

function replacing<T extends object>(value: T, key: string): T {
  return {...value, [key]: replaced()};
}

test('property: a batch changes only the loader\u2019s fields and the record log', () => {
  const contextFields = Object.keys(initial).filter(key => !BATCH_FIELDS.includes(key));
  const uiFields = Object.keys(initial.ui).filter(key => !BATCH_UI_FIELDS.includes(key));
  fc.assert(
    fc.property(
      fc.constantFrom(...contextFields),
      fc.constantFrom(...uiFields),
      fc.subarray(BATCH_UI_FIELDS, {minLength: 1}),
      (field, uiField, logFields) => {
        // Held: everything a batch brings, all at once.
        let batch: GatewayRunContext = {
          ...initial,
          load: undefined,
          replies: {...initial.replies},
          progress: {...HISTORY, through: 2n},
        };
        for (const key of logFields) batch = {...batch, ui: replacing(batch.ui, key)};
        expect(isCatchUpBatch(initial, batch)).toBe(true);
        // Shown at once: any other field changed, alone or with a batch.
        expect(isCatchUpBatch(initial, replacing(initial, field))).toBe(false);
        expect(isCatchUpBatch(initial, replacing(batch, field))).toBe(false);
        expect(isCatchUpBatch(initial, {...initial, ui: replacing(initial.ui, uiField)})).toBe(
          false,
        );
        expect(isCatchUpBatch(initial, {...batch, ui: replacing(batch.ui, uiField)})).toBe(false);
      },
    ),
    {numRuns: 300, seed: 20261030},
  );
});

test('a read that is not catching up is never held, whatever changed', () => {
  const complete = {...initial, progress: {phase: 'complete'} as const};
  const skeleton = {...initial, progress: {phase: 'skeleton'} as const};
  const failed = {...initial, failure: {kind: 'stream-ended'} as const};
  for (const next of [complete, skeleton, failed]) {
    expect(isCatchUpBatch(initial, next)).toBe(false);
    expect(isCatchUpBatch(undefined, next)).toBe(false);
  }
});

test('the first publication of a catching-up read is a batch', () => {
  expect(isCatchUpBatch(undefined, initial)).toBe(true);
});
