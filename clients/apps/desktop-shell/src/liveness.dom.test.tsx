/**
 * What the Run screen says of a run's liveness, from the host's word and the connection: a lock
 * released under a running or a paused run reads Interrupted, finished history needs no
 * connection, and a live run that loses its connection says it is reconnecting. The shell is
 * drawn in a DOM over the journeys' Fake gateway (`openShell` with `run`); the layout of each
 * state is the browser's (`e2e/liveness.spec.ts`).
 */
import {afterAll, afterEach, beforeAll, test} from 'bun:test';
import {create, toBinary} from '@bufbuild/protobuf';
import {RecordSchema, RunEntrySchema, RunLiveness} from '@vibesys/proto';
import {fixtureNamed, storeRunFixture} from '@vibesys/run-fixtures';
import {FakeClock} from '@vibesys/transport/testing';
import {IDBFactory} from 'fake-indexeddb';
import {Window} from 'happy-dom';
import {act} from 'react';
import {notSee, see} from './testing/dom-query.js';
import {seedOf} from './testing/fixture-seed.js';
import {wire} from './testing/gateway-fixture.js';
import {openRun} from './testing/run-screen.js';
import {type OpenShell, openShell} from './testing/shell-world.js';

const window = new Window();
const installed = Object.getOwnPropertyNames(window).filter(name => !(name in globalThis));
beforeAll(() => {
  for (const name of installed)
    Object.defineProperty(globalThis, name, {
      configurable: true,
      writable: true,
      value: Reflect.get(window, name),
    });
  Object.assign(globalThis, {window, IS_REACT_ACT_ENVIRONMENT: true});
});
afterAll(async () => {
  for (const name of [...installed, 'window', 'indexedDB', 'IS_REACT_ACT_ENVIRONMENT'])
    Reflect.deleteProperty(globalThis, name);
  await window.happyDOM.close();
});

let shell: OpenShell | undefined;
afterEach(async () => {
  await shell?.close();
  shell = undefined;
});

/** The run's listing on the dashboard and its replay in the Fake gateway. */
async function opened(kind: 'live' | 'short', liveness: RunLiveness, hold?: boolean) {
  // A fresh copy keeps nothing the last one cached.
  Object.assign(globalThis, {indexedDB: new IDBFactory()});
  const fixture = storeRunFixture(kind);
  const records =
    hold === undefined
      ? fixture.records
      : [
          ...fixture.records,
          create(RecordSchema, {
            seq: (fixture.records.at(-1)?.seq ?? 0n) + 1n,
            epoch: 1,
            at: {seconds: 1700000100n},
            body: {
              case: 'lifecycle',
              value: {
                state: {
                  case: 'running',
                  value: hold ? {hold: {reason: {case: 'operator', value: {}}}} : {},
                },
              },
            },
          }),
        ];
  const input = wire({...fixture, records, initialCount: records.length}, liveness);
  shell = await openShell(null, {
    schedule: new FakeClock().schedule,
    seed: seedOf({...fixtureNamed('finished-run'), runId: 'run-a'}, 'ink'),
    run: {input, updates: {}},
  });
  await openRun(shell);
  return shell;
}

const interrupted = [
  ...toBinary(
    RunEntrySchema,
    create(RunEntrySchema, {liveness: RunLiveness.INTERRUPTED, head: {process: {epoch: 1}}}),
  ),
];

for (const paused of [false, true]) {
  test(`a lock released under a ${paused ? 'paused' : 'running'} run reads Interrupted, not its last state`, async () => {
    const {container, gateway} = await opened('live', RunLiveness.ALIVE, paused);
    await see(container, paused ? 'Paused by you' : 'Running', true);
    await act(async () => gateway?.observe(interrupted));
    await see(container, 'Interrupted', true);
    notSee(container, 'Paused by you', true);
    notSee(container, 'Live', true);
    notSee(container, 'Disconnected', true);
  });
}

test('finished history survives the loss of the host stream', async () => {
  const {container, gateway} = await opened('short', RunLiveness.ENDED);
  await see(container, 'Finished', true);
  await act(async () => gateway?.disconnect());
  await see(container, 'Finished', true);
  notSee(container, 'Disconnected', true);
});

test('a live run that loses its connection says it is reconnecting', async () => {
  const {container, gateway} = await opened('live', RunLiveness.ALIVE);
  await see(container, 'Running', true);
  await act(async () => gateway?.disconnect());
  await see(container, 'Reconnecting', true);
});
