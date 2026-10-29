/**
 * The performance row as the user sees it: with the history loaded through the gateway, the tile's
 * sparkline and the strip chart are drawn exactly when the run holds a measured point, at every
 * prefix of every generated history (one point is a chart too; no point is none).
 */

import {afterAll, beforeAll, expect, test} from 'bun:test';
import {clone, create, fromBinary} from '@bufbuild/protobuf';
import {anyPack} from '@bufbuild/protobuf/wkt';
import {
  Change,
  GateAttrsSchema,
  GateStage,
  GateSubject,
  InputMeasurementSchema,
  MeasuredInputSchema,
  RecordSchema,
  type RunHead,
  RunHeadSchema,
  RuntimeType,
  SpanStatus,
} from '@vibesys/proto';
import {storeRunFixture} from '@vibesys/run-fixtures';
import {foldSkeleton, seedSkeleton} from '@vibesys/run-skeleton';
import {Window} from 'happy-dom';
import {act} from 'react';
import {createRoot} from 'react-dom/client';
import {
  heroView,
  objectivesOf,
  projectSkeleton,
  RUN_ROUTE,
  runPageInitial,
  skeletonScreenState,
} from '../../state/index.js';
import {RunScreen} from './run-screen.js';

const window = new Window();
const installed = Object.getOwnPropertyNames(window).filter(name => !(name in globalThis));

beforeAll(() => {
  for (const name of installed) {
    Object.defineProperty(globalThis, name, {
      configurable: true,
      writable: true,
      value: Reflect.get(window, name),
    });
  }
  Object.assign(globalThis, {window, IS_REACT_ACT_ENVIRONMENT: true});
});

afterAll(async () => {
  for (const name of [...installed, 'window', 'IS_REACT_ACT_ENVIRONMENT']) {
    Reflect.deleteProperty(globalThis, name);
  }
  await window.happyDOM.close();
});

const SEED = {
  identity: {host: 'local' as const, runId: 'run-a'},
  title: {project: 'queue-rs', task: null},
  hostLabel: 'Local',
  unavailable: {},
};

/** What the page shows for `records`: the real projection into the real Run screen. */
async function rendered(
  records: ReturnType<typeof storeRunFixture>['records'],
  head?: (through: bigint) => RunHead,
) {
  const skeleton = foldSkeleton(seedSkeleton(), records);
  const through = skeleton.throughSeq ?? 0n;
  const whole = projectSkeleton(skeleton, through, '', head ? {head: head(through)} : {});
  const state = skeletonScreenState(SEED, skeleton, whole, runPageInitial(SEED));
  const container = document.createElement('div');
  document.body.append(container);
  const root = createRoot(container);
  await act(async () =>
    root.render(
      <RunScreen
        state={state}
        model={whole}
        whole={whole}
        core={state.run.core}
        route={RUN_ROUTE}
        zoom={() => undefined}
        dispatch={() => undefined}
      />,
    ),
  );
  const drawn = {
    sparklines: container.querySelectorAll('.vs-spark-line').length,
    charts: container.querySelectorAll('.vs-chart').length,
    hasProgression: (heroView(whole, objectivesOf(whole)).tiles[0]?.trend?.steps.length ?? 0) > 0,
    points: skeleton.metrics.reduce((sum, series) => sum + series.points.length, 0),
    // Whether the tile claims a measured value: what it shows, the chart must draw from too.
    tileValue: heroView(whole, objectivesOf(whole)).tiles.some(tile => tile.figure !== '\u2013'),
  };
  await act(async () => root.unmount());
  container.remove();
  return drawn;
}

/** The same history with every benchmark gate read as the input's: its points are the input's. */
function inputOnly(records: ReturnType<typeof storeRunFixture>['records']) {
  return records.map(record => {
    const label = record.body.case === 'span' ? record.body.value.span?.label : undefined;
    if (label?.kind !== 'gate' || label.attrs === undefined) return record;
    const copy = clone(RecordSchema, record);
    const gate = fromBinary(GateAttrsSchema, label.attrs.value);
    gate.subject = GateSubject.INPUT;
    const copied = copy.body.value as {span: {label: {attrs: unknown}}};
    copied.span.label.attrs = anyPack(GateAttrsSchema, gate);
    return copy;
  });
}

/** The history with the input measured by a gate of its own, before any idea: no idea carries it. */
function withStandaloneInput(
  records: ReturnType<typeof storeRunFixture>['records'],
  value: number,
) {
  const gate = create(RecordSchema, {
    epoch: 1,
    body: {
      case: 'span',
      value: {
        change: Change.ENDED,
        span: {
          id: 'input-gate',
          runtimeType: RuntimeType.OPERATION,
          name: 'Benchmark',
          status: SpanStatus.DONE,
          startedAt: 0,
          endedAt: 1,
          label: {
            kind: 'gate',
            title: 'Benchmark',
            verdict: 'passed',
            attrs: anyPack(
              GateAttrsSchema,
              create(GateAttrsSchema, {stage: GateStage.BENCHMARK, subject: GateSubject.INPUT}),
            ),
          },
        },
      },
    },
  });
  const point = create(RecordSchema, {
    epoch: 1,
    body: {case: 'metric', value: {series: 'throughput', span: 'input-gate', value, clock: 1}},
  });
  // The writer states the input it measured in the epoch declaration; the chart reads that.
  const opening = clone(RecordSchema, records[0] as (typeof records)[number]);
  if (opening.body.case === 'epochStarted' && opening.body.value.run) {
    opening.body.value.run.inputMeasurement = create(InputMeasurementSchema, {
      state: {
        case: 'measured',
        value: create(MeasuredInputSchema, {series: 'throughput', value, span: 'input-gate'}),
      },
    });
  }
  return [opening, ...records.slice(1, 2), gate, point, ...records.slice(2)].map((record, index) =>
    create(RecordSchema, {...record, seq: BigInt(index + 1)}),
  );
}

test('the tile, the sparkline and the chart read one source, whoever measured the input', async () => {
  for (const count of [0, 1, 3]) {
    const made = storeRunFixture('short', count, 1).records;
    for (const withHead of [false, true]) {
      const records = withStandaloneInput(made, 100);
      for (let length = 4; length <= records.length; length += 3) {
        const head = withHead
          ? (through: bigint): RunHead =>
              create(RunHeadSchema, {
                committedSeq: through,
                objective: {
                  series: {name: 'throughput', unit: 'ops/s'},
                  bestValue: 100,
                  inputValue: 100,
                },
              })
          : undefined;
        const drawn = await rendered(records.slice(0, length), head);
        const label = `${count} ideas, ${length} records, head ${withHead}`;
        // Every measured point is drawn, and a tile that shows a value never stands alone.
        expect(drawn.points, label).toBeGreaterThan(0);
        expect(drawn.sparklines, `sparkline: ${label}`).toBeGreaterThan(0);
        expect(drawn.charts, `chart: ${label}`).toBeGreaterThan(0);
        if (drawn.tileValue)
          expect(drawn.charts, `tile without chart: ${label}`).toBeGreaterThan(0);
      }
    }
  }
});

test('raw readings remain charted without inventing a best progression', async () => {
  for (const kind of ['short', 'live', 'facts'] as const) {
    for (const count of [1, 3]) {
      const made = storeRunFixture(kind, count, 1).records;
      for (const [whose, records] of [
        ['candidate', made],
        ['input', inputOnly(made)],
      ] as const)
        for (let length = 1; length <= records.length; length += 3) {
          const drawn = await rendered(records.slice(0, length));
          const label = `${kind} ${count} ideas, ${length} records, ${whose} points`;
          expect(drawn.sparklines > 0, `sparkline: ${label}`).toBe(drawn.hasProgression);
          expect(drawn.charts > 0, `chart: ${label}`).toBe(drawn.points > 0);
        }
    }
  }
});
