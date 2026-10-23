/**
 * The performance row as the user sees it: with the history loaded through the gateway, the tile's
 * sparkline and the strip chart are drawn exactly when the run holds a measured point, at every
 * prefix of every generated history (one point is a chart too; no point is none).
 */

import {afterAll, beforeAll, expect, test} from 'bun:test';
import {clone, fromBinary} from '@bufbuild/protobuf';
import {anyPack} from '@bufbuild/protobuf/wkt';
import {GateAttrsSchema, GateSubject, RecordSchema} from '@vibesys/proto';
import {storeRunFixture} from '@vibesys/run-fixtures';
import {foldSkeleton, seedSkeleton} from '@vibesys/run-skeleton';
import {Window} from 'happy-dom';
import {act} from 'react';
import {createRoot} from 'react-dom/client';
import {
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
async function rendered(records: ReturnType<typeof storeRunFixture>['records']) {
  const skeleton = foldSkeleton(seedSkeleton(), records);
  const whole = projectSkeleton(skeleton, skeleton.throughSeq ?? 0n);
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
    points: skeleton.metrics.reduce((sum, series) => sum + series.points.length, 0),
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

test('the sparkline and the chart are drawn iff the history holds a measured point', async () => {
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
          expect(drawn.sparklines > 0, `sparkline: ${label}`).toBe(drawn.points > 0);
          expect(drawn.charts > 0, `chart: ${label}`).toBe(drawn.points > 0);
        }
    }
  }
});
