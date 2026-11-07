import {create} from '@bufbuild/protobuf';
import {timestampFromMs} from '@bufbuild/protobuf/wkt';
import {describe, expect, it} from '@effect/vitest';
import {Direction, SearchStateSchema} from '@vibesys/orchestrator-proto';
import fc from 'fast-check';
import {applyHostFrame, emptyHostModel} from '../host-model.js';
import {objectiveBounds} from './search-views.js';

interface Declared {
  readonly seq: bigint;
  readonly value: number;
  readonly unit: string;
  readonly note: string;
  readonly agent: string;
  readonly at: number;
}

const declared = fc
  .uniqueArray(fc.bigInt({min: 1n, max: 500n}), {minLength: 1, maxLength: 8})
  .map(seqs => seqs.sort((a, b) => (a < b ? -1 : 1)))
  .chain(seqs =>
    fc.tuple(
      ...seqs.map(seq =>
        fc.record({
          seq: fc.constant(seq),
          value: fc.double({min: 1e-3, max: 1e9, noNaN: true}),
          unit: fc.constantFrom('tok/s', 'ms', 'GB'),
          note: fc.string({maxLength: 40}),
          agent: fc.constantFrom('lead', 'planner-2', ''),
          at: fc.integer({min: 1, max: 100000}),
        }),
      ),
    ),
  );

const served = (objective: string, versions: readonly Declared[], extra = {}) => ({
  objective,
  direction: Direction.MAXIMIZE,
  versions: versions.map(v => ({
    seq: v.seq,
    objective,
    direction: Direction.MAXIMIZE,
    value: v.value,
    unit: v.unit,
    note: v.note,
    origin: {runId: 'r', agent: v.agent, at: timestampFromMs(v.at * 1000)},
  })),
  ...extra,
});

describe('declared performance bounds', () => {
  it('show every version in the order served, the last as the current bound, and the served numbers as they are', () => {
    fc.assert(
      fc.property(
        declared,
        fc.option(fc.double({min: 1, max: 1e9, noNaN: true}), {nil: undefined}),
        fc.option(fc.double({min: 1, max: 500, noNaN: true}), {nil: undefined}),
        fc.option(fc.double({min: 1, max: 500, noNaN: true}), {nil: undefined}),
        (versions, headValue, percentOfBound, bestPercentOfBound) => {
          const search = create(SearchStateSchema, {
            bounds: [served('tok_s', versions, {headValue, percentOfBound, bestPercentOfBound})],
          });
          const [view] = objectiveBounds(search);
          expect(view?.objective).toBe('tok_s');
          expect(view?.direction).toBe(Direction.MAXIMIZE);
          expect(view?.history.map(v => v.seq)).toEqual(versions.map(v => v.seq));
          expect(view?.history.map(v => v.atMs)).toEqual(versions.map(v => v.at * 1000));
          expect(view?.current).toBe(view?.history.at(-1));
          expect(view?.current.value).toBe(versions.at(-1)?.value);
          // The app shows what the orchestrator served and derives nothing: with no served percent
          // there is none, whatever the numbers beside it would give.
          expect(view?.headValue).toBe(headValue);
          expect(view?.percentOfBound).toBe(percentOfBound);
          expect(view?.bestPercentOfBound).toBe(bestPercentOfBound);
        },
      ),
      {seed: 49},
    );
  });

  it('keep the objectives in the order served, and none for a run that declared none', () => {
    const one = (objective: string) =>
      served(objective, [{seq: 1n, value: 1, unit: 'u', note: '', agent: 'lead', at: 1}]);
    const search = create(SearchStateSchema, {bounds: ['mem_gb', 'tok_s', 'ttft_ms'].map(one)});
    expect(objectiveBounds(search).map(view => view.objective)).toEqual([
      'mem_gb',
      'tok_s',
      'ttft_ms',
    ]);
    expect(objectiveBounds(create(SearchStateSchema))).toEqual([]);
  });

  it('follow the search frames: a revision adds a version, a stale frame changes nothing', () => {
    const frame = (revision: bigint, versions: readonly Declared[], percentOfBound: number) =>
      create(SearchStateSchema, {
        runId: 'r',
        revision,
        bounds: [served('tok_s', versions, {headValue: 7800, percentOfBound})],
      });
    const first = {seq: 1n, value: 15600, unit: 'tok/s', note: 'bandwidth', agent: 'lead', at: 10};
    const second = {seq: 2n, value: 10000, unit: 'tok/s', note: 'KV', agent: 'planner-2', at: 20};
    const show = (model: typeof emptyHostModel) =>
      objectiveBounds(model.data.searches['r'] ?? create(SearchStateSchema));

    const initial = applyHostFrame(emptyHostModel, {
      from: 'runs',
      body: {case: 'search', value: frame(1n, [first], 50)},
    });
    expect(show(initial).map(v => [v.current.value, v.history.length, v.percentOfBound])).toEqual([
      [15600, 1, 50],
    ]);

    const revised = applyHostFrame(initial, {
      from: 'runs',
      body: {case: 'search', value: frame(2n, [first, second], 78)},
    });
    expect(
      show(revised).map(v => [v.current.value, v.history.map(h => h.agent), v.percentOfBound]),
    ).toEqual([[10000, ['lead', 'planner-2'], 78]]);

    const stale = applyHostFrame(revised, {
      from: 'runs',
      body: {case: 'search', value: frame(1n, [first], 50)},
    });
    expect(stale.data.searches).toBe(revised.data.searches);
    expect(show(stale)).toEqual(show(revised));
  });
});
