import {create} from '@bufbuild/protobuf';
import {describe, expect, it} from '@effect/vitest';
import {EventSchema, RunsSnapshotSchema, SearchStateSchema} from '@vibesys/orchestrator-proto';
import fc from 'fast-check';
import {applyHostFrame, beginRunsStream, emptyHostModel} from '../host-model.js';
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
  it('a snapshot followed by states keeps the first greatest revision per run', () => {
    fc.assert(
      fc.property(facts, rows => {
        const snapshot = create(RunsSnapshotSchema, {searches: rows.slice(0, 1)});
        const actual = rows.reduce(applySearch, applySnapshot(emptyState, snapshot));
        const expected = Object.fromEntries(
          ['a', 'b', 'c'].flatMap(runId => {
            const newest = rows
              .filter(row => row.runId === runId)
              .sort((a, b) => (a.revision > b.revision ? -1 : a.revision < b.revision ? 1 : 0))[0];
            return newest === undefined ? [] : [[runId, newest]];
          }),
        );
        expect(actual.searches).toEqual(expected);
      }),
      {seed: 19},
    );
  });
  it('equal revisions retain the first facts, including different content', () => {
    fc.assert(
      fc.property(fc.nat(), fc.nat(), (first, second) => {
        const row = create(SearchStateSchema, {runId: 'r', revision: 5n, unattributedJobs: first});
        const state = applySearch(emptyState, row);
        expect(
          applySearch(state, create(SearchStateSchema, {...row, unattributedJobs: second})),
        ).toBe(state);
      }),
    );
  });
  it('each stream shows its last increasing view and retains facts before its first frame', () => {
    fc.assert(
      fc.property(
        fc.array(
          fc.array(
            fc.record({
              step: fc.bigInt({min: 1n, max: 1000n}),
              unattributedJobs: fc.nat({max: 100}),
            }),
            {maxLength: 10},
          ),
          {maxLength: 10},
        ),
        streams => {
          let model = emptyHostModel;
          for (const values of streams) {
            const before = model.data.searches;
            model = beginRunsStream(model);
            expect(model.data.searches).toBe(before);
            let revision = 0n;
            values.forEach(({step, unattributedJobs}) => {
              revision += step;
              const view = create(SearchStateSchema, {
                runId: 'r',
                revision,
                unattributedJobs,
              });
              model = applyHostFrame(model, {from: 'runs', body: {case: 'search', value: view}});
              expect(model.data.searches['r']).toEqual(view);
            });
          }
        },
      ),
      {seed: 69},
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

it('search frames preserve the cursor and gap in arbitrary snapshot and event interleavings', () => {
  fc.assert(
    fc.property(
      fc.array(
        fc.oneof(
          fc.record({kind: fc.constant('snapshot'), sequence: fc.bigInt({min: 0n, max: 20n})}),
          fc.record({kind: fc.constant('event'), sequence: fc.bigInt({min: 1n, max: 25n})}),
          fc.record({kind: fc.constant('search'), revision: fc.bigInt({min: 1n, max: 20n})}),
        ),
        {maxLength: 80},
      ),
      steps => {
        let model = emptyHostModel;
        let withoutSearch = emptyHostModel;
        for (const step of steps) {
          if (step.kind === 'search') {
            const before = model.data;
            model = applyHostFrame(model, {
              from: 'runs',
              body: {
                case: 'search',
                value: create(SearchStateSchema, {runId: 'r', revision: step.revision}),
              },
            });
            expect(model.data.sequence).toBe(before.sequence);
            expect(model.data.gap).toBe(before.gap);
          } else {
            const body =
              step.kind === 'snapshot'
                ? {
                    case: 'snapshot' as const,
                    value: create(RunsSnapshotSchema, {sequence: step.sequence}),
                  }
                : {case: 'event' as const, value: create(EventSchema, {sequence: step.sequence})};
            model = applyHostFrame(model, {from: 'runs', body});
            withoutSearch = applyHostFrame(withoutSearch, {from: 'runs', body});
          }
          expect(model.data.sequence).toBe(withoutSearch.data.sequence);
          expect(model.data.gap).toEqual(withoutSearch.data.gap);
        }
      },
    ),
    {seed: 73},
  );
});
