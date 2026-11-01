/**
 * The Run screen over a store run's records: it reads the skeleton and the blobs once, whatever
 * is scrubbed or shown after, walks every face and the idea and step levels, says what a finished
 * run offers (Resume) and what the Usage face keeps (model and dates), and holds no stream once
 * the person leaves it. The shell is drawn in a DOM over the journeys' Fake gateway; the layout of
 * each of these states is the browser's (`e2e/gateway-records.spec.ts`).
 */
import {afterAll, afterEach, beforeAll, expect, test} from 'bun:test';
import {storeRunFixture} from '@vibesys/run-fixtures';
import {FakeClock} from '@vibesys/transport/testing';
import {Window} from 'happy-dom';
import {act} from 'react';
import {fillIn, press} from './testing/dom-controls.js';
import {all, maybe, notSee, one, see, settle, until} from './testing/dom-query.js';
import {shellOnRun, storedWorld} from './testing/run-screen.js';
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
});

const asked = (opened: OpenShell, method: string): number =>
  (opened.gateway?.calls() ?? []).filter(call => call.endsWith(`/${method}`)).length;

const show = async (container: Element, face: string): Promise<void> =>
  press(one(container, {role: 'radio', name: face, exact: true}));

for (const kind of ['short', 'live', 'blobs'] as const) {
  test(`a ${kind} store run reads its skeleton and blobs once, and shows every face and level`, async () => {
    shell = await shellOnRun(storedWorld(storeRunFixture(kind)), new FakeClock().schedule);
    const {container} = shell;
    expect(maybe(container, {role: 'switch', name: 'Run transport v2'})).toBeNull();
    one(container, {role: 'heading', name: 'queue-rs · mpmc'});
    expect(all(container, {role: 'button', name: /Idea 1/}).length).toBeGreaterThan(0);
    const range = one(container, {role: 'slider'});
    // The blobs a run holds are each read once, and no more.
    const blobs = kind === 'blobs' ? 2 : 1;
    const opened = shell;
    await until('the blobs read', () => asked(opened, 'GetBlobs') >= blobs);
    await settle();
    expect(asked(shell, 'GetBlobs')).toBe(blobs);
    await fillIn(range as HTMLInputElement, '2');
    await settle();
    expect(asked(shell, 'Skeleton')).toBe(1);
    expect(asked(shell, 'GetBlobs')).toBe(blobs);
    await press(
      one(container, {role: 'button', name: kind === 'live' ? 'Back to live' : 'Jump to end'}),
    );
    for (const face of ['Timeline', 'Agents', 'Usage', 'Logs']) {
      await show(container, face);
      if (face === 'Logs') await see(container, 'Build complete', true);
    }
    await show(container, 'Ideas');
    await press(one(container, {role: 'option', name: /^Idea 1(?!\d)/}));
    const steps = one(container, {role: 'listbox', name: /steps$/});
    await press(all(steps, {role: 'option', name: /builder/i})[0] as HTMLElement);
    one(container, {role: 'region', name: 'Implement', exact: true});
    await see(container, 'No transcript', true);
    await press(one(container, {role: 'button', name: 'Settings', exact: true}));
    // Leaving the run releases every stream it held.
    expect(shell.gateway?.active()).toBe(0);
  });
}

test('native history opens without legacy history, usage keeps its model and dates, and a finished run offers Resume', async () => {
  shell = await shellOnRun(storedWorld(storeRunFixture('short')), new FakeClock().schedule);
  const {container} = shell;
  expect(maybe(container, {role: 'switch', name: 'Run transport v2'})).toBeNull();
  await show(container, 'Usage');
  await show(container, 'Model');
  await show(container, 'Tokens');
  await see(container, 'model-a', true);
  notSee(container, /turns have no end time/);
  // A finished run offers Resume: every ending can be resumed.
  one(container, {role: 'button', name: 'Resume', exact: true});
  await act(async () => Promise.resolve());
});
