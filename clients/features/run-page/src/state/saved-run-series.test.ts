/** Field shapes: detached evaluator gates, owned measurement gates, and runs with no idea spans. */
import {expect, test} from 'bun:test';
import {clone, create, toBinary} from '@bufbuild/protobuf';
import {Direction, RecordSchema, RunHeadSchema, SkeletonResponseSchema} from '@vibesys/proto';
import {storeRunFixture} from '@vibesys/run-fixtures';
import {ingestSkeleton, seedSkeleton} from '@vibesys/run-skeleton';
import {
  clockStart,
  headerView,
  heroView,
  paintedSkeleton,
  projectSkeleton,
  runPageInitial,
  skeletonScreenState,
  } from './index.js';
import {firstChart} from './chart-fixtures.js';

for (let seed = 1; seed <= 12; seed += 1) {
  test(`saved series survive missing idea ownership and catch-up (seed ${seed})`, () => {
    const minimize = seed % 2 === 0;
    const records = savedRecords(seed, minimize);
    const committedSeq = records.at(-1)?.seq ?? 0n;
    const values = records.flatMap(record =>
      record.body.case === 'metric' && record.body.value.series === 'throughput'
        ? [record.body.value.value]
        : [],
    );
    expect(values.length).toBeGreaterThan(0);
    const best = undefined;
    const head = create(RunHeadSchema, {
      committedSeq,
      objective: {series: {name: 'throughput'}, bestValue: best, inputValue: 100},
    });
    const load = ingestSkeleton(
      create(SkeletonResponseSchema, {
        records: records.map(record => toBinary(RecordSchema, record)),
        nextSeq: committedSeq + 1n,
        committedSeq,
        complete: true,
      }),
    );
    const model = projectSkeleton(load.skeleton, committedSeq, '', {head});
    const chart = firstChart(model, clockStart(model), model.latest);
    expect(chart.readings.map(point => point.y).sort((a, b) => a - b)).toEqual(
      [...values].sort((a, b) => a - b),
    );
    // No reading beat the input, so the line is the input's, flat, and no step is claimed.
    expect(chart.steps.at(-1)?.y).toBe(100);
    expect(new Set(chart.steps.map(step => step.y))).toEqual(new Set([100]));
    expect(new Set(chart.readings.map(point => point.key)).size).toBe(values.length);
    expect(heroView(model).headlines[0]?.figure).toBe('–');
    expect(heroView(model).headlines[0]?.words).toBe('Unavailable');
    expect(paintedSkeleton(load)).toBe(load.skeleton);
    expect(paintedSkeleton({...load, complete: false})).toBe(load.skeleton);
    const prefix = projectSkeleton(load.skeleton, committedSeq, '', {
      progress: {phase: 'history', through: committedSeq, committed: committedSeq + 1n},
    });
    expect(heroView(prefix).headlines[0]).toMatchObject({
      figure: '–',
      ratio: null,
      note: 'Catching up',
    });
    const pageSeed = {
      identity: {host: 'local' as const, runId: 'run-a'},
      title: {project: 'queue-rs', task: null},
      hostLabel: 'Local',
      unavailable: {},
    };
    const state = skeletonScreenState(pageSeed, load.skeleton, prefix, runPageInitial(pageSeed));
    expect(state.progress).toEqual(prefix.progress);
    expect(state.positionWord).toBe('Catching up');
    expect(headerView(state).spent).toBeNull();
  });
}

test('an empty complete response still waits for an epoch', () => {
  expect(
    paintedSkeleton({
      skeleton: seedSkeleton(),
      complete: true,
      nextSeq: 0n,
      committedSeq: 0n,
      windowFrom: undefined,
      failuresBefore: undefined,
    }),
  ).toBeUndefined();
});

function savedRecords(seed: number, minimize: boolean) {
  const records = storeRunFixture('facts', 1 + (seed % 4), seed)
    .records.map(record => clone(RecordSchema, record))
    .filter(record => seed % 3 !== 0 || !isWorkstream(record));
  for (const [index, record] of records.entries()) {
    record.seq = BigInt(index + 1);
    setDirection(record, minimize);
    if (
      seed % 3 === 1 &&
      record.body.case === 'span' &&
      record.body.value.span?.label?.kind === 'gate'
    ) {
      record.body.value.span.parent = undefined;
    }
    if (record.body.case === 'metric' && record.body.value.series === 'throughput') {
      if (record.body.value.span !== 'input-gate') record.body.value.value += seed;
    }
  }
  return records;
}

function isWorkstream(record: ReturnType<typeof storeRunFixture>['records'][number]) {
  return record.body.case === 'span' && record.body.value.span?.label?.kind === 'workstream';
}

function setDirection(
  record: ReturnType<typeof storeRunFixture>['records'][number],
  minimize: boolean,
) {
  if (record.body.case === 'epochStarted' || record.body.case === 'declarationAmended') {
    const objective = record.body.value.run?.objectives[0];
    if (objective) objective.direction = minimize ? Direction.MIN : Direction.MAX;
  }
}
