/** Arbitraries for the search views the report tests fold: served series, and the reports made of them. */
import {create, type MessageInitShape} from '@bufbuild/protobuf';
import {timestampFromMs} from '@bufbuild/protobuf/wkt';
import {Direction, type SearchState, SearchStateSchema} from '@vibesys/orchestrator-proto';
import fc from 'fast-check';
import {type ObjectiveReport, objectiveReports} from '../../state/index.js';

type ServedSeries = NonNullable<MessageInitShape<typeof SearchStateSchema>['series']>;

/** A measure job has one time whichever objectives it measured: here, a function of its number. */
const point = fc
  .record({
    n: fc.integer({min: 1, max: 30}),
    commit: fc.stringMatching(/^[0-9a-f]{7}$/),
    value: fc.double({min: 1, max: 1e6, noNaN: true}),
    bestSoFar: fc.boolean(),
  })
  .map(({n, ...rest}) => ({...rest, jobId: `job-${n}`, seconds: n * 1000}));

/** Served series of one to three objectives; their points are oldest first, as the host sends them. */
export const servedSeries: fc.Arbitrary<ServedSeries> = fc.uniqueArray(
  fc.record({
    objective: fc.constantFrom('tok_s', 'p99_ms', 'mem_gb'),
    direction: fc.constantFrom(Direction.MAXIMIZE, Direction.MINIMIZE),
    points: fc
      .uniqueArray(point, {selector: each => each.jobId, minLength: 1, maxLength: 25})
      .map(points =>
        [...points]
          .sort((a, b) => a.seconds - b.seconds)
          .map(({seconds, ...rest}) => ({...rest, at: timestampFromMs(seconds * 1000)})),
      ),
  }),
  {selector: row => row.objective, minLength: 1, maxLength: 3},
);

/** A search view that serves `series`, cut short or not. */
export const searchOf = (series: ServedSeries, truncated = false): SearchState =>
  create(SearchStateSchema, {series, truncated});

/** The objective reports of a served series. */
export const reportsOf = (search: SearchState): readonly ObjectiveReport[] =>
  objectiveReports(search, []);
