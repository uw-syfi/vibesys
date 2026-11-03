import {create} from '@bufbuild/protobuf';
import {describe, expect, it} from '@effect/vitest';
import {RunsSnapshotSchema, SearchStateSchema} from '@vibesys/orchestrator-proto';
import fc from 'fast-check';
import {applyHostFrame, emptyHostModel} from '../host-model.js';
import {applySearch, applySnapshot} from './fold.js';
import {emptyState} from './state.js';

const facts = fc
  .uniqueArray(
    fc.record({
      runId: fc.constantFrom('a', 'b', 'c'),
      revision: fc.bigInt({min: 1n, max: 10000n}),
      unattributedJobs: fc.nat({max: 100}),
    }),
    {selector: row => `${row.runId}/${row.revision}`, maxLength: 40},
  )
  .map(rows => rows.map(row => create(SearchStateSchema, row)));

describe('whole search facts', () => {
  it('repeats and reordered revisions converge to the same newest facts per run', () => {
    fc.assert(
      fc.property(facts, rows => {
        const expected = rows.reduce(applySearch, emptyState);
        expect([...rows].reverse().concat(rows).reduce(applySearch, emptyState)).toEqual(expected);
        for (const row of rows) expect(applySearch(expected, row)).toBe(expected);
      }),
      {seed: 9},
    );
  });
  it('a snapshot followed by states equals a snapshot of those facts', () => {
    fc.assert(
      fc.property(facts, rows => {
        const snapshot = create(RunsSnapshotSchema, {searches: rows.slice(0, 1)});
        const actual = rows.reduce(applySearch, applySnapshot(emptyState, snapshot));
        expect(actual).toEqual(
          applySnapshot(
            emptyState,
            create(RunsSnapshotSchema, {searches: Object.values(actual.searches)}),
          ),
        );
      }),
      {seed: 19},
    );
  });
  it('a new stream snapshot resets revisions, even below the held event sequence', () => {
    const old = {
      ...applySearch(emptyState, create(SearchStateSchema, {runId: 'r', revision: 99n})),
      sequence: 100n,
    };
    expect(applySnapshot(old, create(RunsSnapshotSchema)).searches).toEqual({});
    const restarted = create(SearchStateSchema, {runId: 'r', revision: 1n});
    expect(
      applySnapshot(old, create(RunsSnapshotSchema, {searches: [restarted]})).searches,
    ).toEqual({r: restarted});
    const model = applyHostFrame(emptyHostModel, {
      from: 'runs',
      body: {case: 'search', value: restarted},
    });
    expect(model.data.searches).toEqual({r: restarted});
  });
});
