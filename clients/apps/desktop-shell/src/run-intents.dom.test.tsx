/**
 * What the Run screen's controls do and say: Pause, Pause now, guidance and Resume post their intent
 * to the host, a row says what became of each (Pending, Refused, none once applied), the journal's
 * outcome moves the run's state, and the controls follow it through pause, resume and the finish.
 * The shell is drawn in a DOM over the journeys' Fake gateway; where the controls sit at each width
 * and state is the browser's (`e2e/run-intents.spec.ts`).
 */
import {afterAll, afterEach, beforeAll, expect, test} from 'bun:test';
import {create} from '@bufbuild/protobuf';
import {IntentAction, IntentStep, Outcome, RecordSchema, RunLiveness} from '@vibesys/proto';
import {fixtureNamed, INTENT_REPLAYS} from '@vibesys/run-fixtures';
import {FakeClock} from '@vibesys/transport/testing';
import {Window} from 'happy-dom';
import {act} from 'react';
import {fillIn, press} from './testing/dom-controls.js';
import {maybe, notSee, one, see, settle, until} from './testing/dom-query.js';
import {
  finishOpenSpans,
  type IntentsWorld,
  intentsWorld,
  observationOf,
  refusedResumeWorld,
  tailOf,
} from './testing/intents-world.js';
import {shellOnRun} from './testing/run-screen.js';
import type {OpenShell} from './testing/shell-world.js';

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
  Reflect.deleteProperty(window, 'postIntent');
});

const LIVE = {...fixtureNamed('queue-rs-live'), runId: 'run-a'};

/** The window's call to the host for an intent, answered by `world`. */
function hostOf(call: (host: string, bytes: number[]) => number[]): void {
  Object.assign(window, {
    postIntent: (host: string, bytes: number[]) => Promise.resolve(call(host, bytes)),
  });
}

async function opened(
  options: {amendment?: boolean; history?: 'pending'} = {},
): Promise<{world: IntentsWorld; shell: OpenShell; clock: FakeClock}> {
  const world = intentsWorld(options);
  const clock = new FakeClock();
  hostOf(world.postIntent);
  shell = await shellOnRun(
    {input: world.input, updates: {}},
    clock.schedule,
    LIVE,
    options.history === 'pending' ? 'as shown' : 'read',
  );
  if (options.history !== 'pending')
    one(shell.container, {role: 'button', name: 'Guidance', exact: true});
  return {world, shell, clock};
}

const button = (container: Element, name: string) =>
  one(container, {role: 'button', name, exact: true});

/** Delivers the journal's records to the Fake gateway, as the host's tail does. */
const deliver = (bytes: number[]) => act(async () => shell?.gateway?.append(bytes));
const observe = (epoch: number, liveness: RunLiveness) =>
  act(async () => shell?.gateway?.observe(observationOf(epoch, liveness)));

test('declaration amendments keep the lifecycle and an addressed Pause', async () => {
  const {world, shell} = await opened({amendment: true});
  const {container} = shell;
  notSee(container, 'Saved run', true);
  expect(maybe(container, {role: 'button', name: 'Resume', exact: true})).toBeNull();
  await press(button(container, 'Pause'));
  await see(container, 'Pause · Pending', true);
  expect(world.requests).toHaveLength(1);
  expect(world.requests[0]?.request.projectRoot).toBe('/work/queue-rs');
  expect(world.requests[0]?.request.expectedEpoch).toBe(1n);
});

test('Pause escalates to Pause now in the same epoch and waits for journal outcomes', async () => {
  const {world, shell} = await opened();
  const {container} = shell;
  await press(button(container, 'Pause'));
  await see(container, 'Pause · Pending', true);
  expect(world.requests[0]).toMatchObject({
    host: 'ssh:remote-host',
    request: {runId: 'run-a', projectRoot: '/work/queue-rs', intent: {case: 'pause'}},
  });
  await deliver(world.outcome(0, 'applied'));
  await see(container, 'Pausing', true);
  // An applied request has no row, and the pending one is gone.
  notSee(container, /^Pause ·/);
  await press(button(container, 'Pause now'));
  await see(container, 'Pause now · Pending', true);
  expect(world.requests[1]?.request.intent.case).toBe('pauseNow');
  expect(world.requests[1]?.request.requestId).not.toBe(world.requests[0]?.request.requestId);
  await deliver(world.outcome(1, 'applied'));
  await see(container, 'Paused', true);
  notSee(container, /^Pause now ·/);
  await press(button(container, 'Settings'));
  await until(
    'the controls gone with the run',
    () => maybe(container, {role: 'button', name: 'Guidance', exact: true}) === null,
  );
  // Leaving the run releases every stream it held.
  expect(shell.gateway?.active()).toBe(0);
});

for (const kind of INTENT_REPLAYS) {
  test(`intent ${kind}: the row says what became of the request`, async () => {
    const {world, shell} = await opened();
    const {container} = shell;
    await press(button(container, 'Pause'));
    if (kind !== 'pending') {
      await see(container, 'Pause · Pending', true);
      await deliver(world.outcome(0, kind === 'park-applied' ? 'applied' : kind));
    }
    // A request still on its way, or refused, keeps its row; an applied one has none.
    const status = kind === 'pending' ? 'Pending' : kind === 'refused' ? 'Refused' : null;
    if (status === null) {
      await settle();
      notSee(container, /^Pause ·/);
    } else await see(container, `Pause · ${status}`, true);
    expect(world.requests).toHaveLength(1);
    if (kind === 'park-applied') {
      expect(world.requests[0]?.request.intent.case).toBe('pause');
      await see(container, 'Pausing', true);
    }
  });
}

test('guidance opens beside the controls and posts the typed text', async () => {
  const {world, shell} = await opened();
  const {container} = shell;
  await press(button(container, 'Guidance'));
  const dialog = one(container, {role: 'dialog', name: 'Guidance', exact: true});
  await fillIn(
    one(dialog, {role: 'textbox', name: 'Guidance', exact: true}) as HTMLTextAreaElement,
    'Measure contention',
  );
  await press(one(dialog, {role: 'button', name: 'Send guidance'}));
  await until(
    'the dialog closed',
    () => maybe(container, {role: 'dialog', name: 'Guidance', exact: true}) === null,
  );
  await see(container, 'Guidance · Pending', true);
  expect(world.requests[0]?.request.intent).toMatchObject({
    case: 'steer',
    value: {text: 'Measure contention'},
  });
});

test('live controls follow pause, resume and finish through the journal', async () => {
  const {world, shell} = await opened();
  const {container} = shell;
  const pauseNow = () => maybe(container, {role: 'button', name: 'Pause now', exact: true});
  expect(pauseNow()?.hasAttribute('disabled')).toBe(false);
  await press(button(container, 'Pause'));
  await see(container, 'Pause · Pending', true);
  await deliver(world.outcome(0, 'applied'));
  await see(container, 'Pausing', true);
  expect(pauseNow()?.hasAttribute('disabled')).toBe(false);
  await press(pauseNow() as HTMLElement);
  await see(container, 'Pause now · Pending', true);
  await deliver(world.outcome(1, 'applied'));
  await observe(1, RunLiveness.ENDED);
  await see(container, 'Paused', true);
  expect(button(container, 'Resume').hasAttribute('disabled')).toBe(false);
  expect(pauseNow()).toBeNull();
  await press(button(container, 'Resume'));
  const header = world.fixture.records.find(record => record.body.case === 'epochStarted');
  if (header?.body.case !== 'epochStarted') throw new Error('epoch header required');
  await deliver(
    tailOf([
      create(RecordSchema, {
        seq: world.next(),
        epoch: 2,
        body: {case: 'epochStarted', value: {...header.body.value, epoch: 2}},
      }),
      create(RecordSchema, {
        seq: world.next(),
        epoch: 2,
        body: {case: 'lifecycle', value: {state: {case: 'running', value: {}}}},
      }),
      create(RecordSchema, {
        seq: world.next(),
        epoch: 2,
        body: {
          case: 'intent',
          value: {controlId: 'resume-2', action: IntentAction.RESUME, step: IntentStep.APPLIED},
        },
      }),
    ]),
  );
  await observe(2, RunLiveness.ALIVE);
  await see(container, 'Running', true);
  await until('Pause now enabled', () => pauseNow()?.hasAttribute('disabled') === false);
  // The applied Resume has no row once the journal's record of it is folded.
  await settle();
  notSee(container, / · Applied/);
  await deliver(
    tailOf([
      ...finishOpenSpans(world.fixture.records, world.next),
      create(RecordSchema, {
        seq: world.next(),
        epoch: 2,
        body: {
          case: 'lifecycle',
          value: {state: {case: 'ended', value: {outcome: Outcome.FINISHED, resumable: false}}},
        },
      }),
    ]),
  );
  await observe(2, RunLiveness.ENDED);
  await until('the controls of a finished run', () => pauseNow() === null);
  expect(maybe(container, {role: 'button', name: 'Pause', exact: true})).toBeNull();
  // The host admits Resume after any ending, whatever flag the ended record keeps.
  button(container, 'Resume');
  notSee(container, 'Running', true);
});

test('Pause now is reachable while a live run is still loading history', async () => {
  const {world, shell} = await opened({history: 'pending'});
  const {container} = shell;
  await see(container, /^History \d+ of \d+$/);
  const pauseNow = button(container, 'Pause now');
  expect(pauseNow.hasAttribute('disabled')).toBe(false);
  await press(pauseNow);
  await see(container, 'Pause now · Pending', true);
  expect(world.requests[0]?.request.intent.case).toBe('pauseNow');
  await deliver(tailOf(world.fixture.records.slice(world.fixture.initialCount)));
  await until('the history read', () => !/History \d+ of \d+/.test(container.textContent ?? ''));
  expect(button(container, 'Pause now').hasAttribute('disabled')).toBe(false);
  await see(container, 'Pause now · Pending', true);
});

test('a pause drain confirmation survives the run reader reconnecting', async () => {
  const {world, shell, clock} = await opened();
  const {container} = shell;
  await press(button(container, 'Pause'));
  await see(container, 'Pause · Pending', true);
  await act(async () => {
    shell.gateway?.disconnect();
    // The reader backs off and reads again from its cursor.
    clock.advanceBy(30_000);
    (
      globalThis.window as unknown as {fakeHost: {recent(runs: readonly unknown[]): void}}
    ).fakeHost.recent([]);
  });
  await deliver(world.outcome(0, 'applied'));
  expect(world.requests).toHaveLength(1);
  await see(container, 'Pausing', true);
  await act(async () => clock.advanceBy(30_000));
  await settle();
  notSee(container, /^Pause ·/);
});

test('a catalog-addressed historical run offers Resume without optional epoch context', async () => {
  const world = refusedResumeWorld();
  hostOf(world.postIntent);
  const {wire} = await import('./testing/gateway-fixture.js');
  shell = await shellOnRun({input: wire(world.fixture), updates: {}}, new FakeClock().schedule);
  await press(button(shell.container, 'Resume'));
  await see(shell.container, 'Resume · Refused: checkout missing', true);
  expect(world.requests).toHaveLength(1);
  expect(world.requests[0]).toMatchObject({projectRoot: '/work/queue-rs', runId: 'run-a'});
});
