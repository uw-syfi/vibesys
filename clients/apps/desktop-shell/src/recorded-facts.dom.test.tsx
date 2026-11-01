/**
 * What the Run screen says of the facts a run recorded: Pause when the log recorded a project root,
 * and still Pause when it recorded none (the catalog addresses the run), the checkout skip on an
 * adoption step, and the word "scripted" on a scripted turn. The shell is drawn in a DOM over the
 * journeys' Fake gateway; the layout of these screens is the browser's (`e2e/recorded-facts.spec.ts`).
 */
import {afterAll, afterEach, beforeAll, test} from 'bun:test';
import {fixtureNamed, storeRunFixture} from '@vibesys/run-fixtures';
import {FakeClock} from '@vibesys/transport/testing';
import {Window} from 'happy-dom';
import {press} from './testing/dom-controls.js';
import {one, see} from './testing/dom-query.js';
import {withFacts, withoutRoot} from './testing/recorded-facts.js';
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

const LIVE = {...fixtureNamed('queue-rs-live'), runId: 'run-a'};

test('a recorded root shows Pause, the word scripted, and the checkout skip of an adoption', async () => {
  shell = await shellOnRun(
    storedWorld(withFacts(storeRunFixture('live'))),
    new FakeClock().schedule,
    LIVE,
  );
  const {container} = shell;
  one(container, {role: 'button', name: 'Pause', exact: true});
  await see(container, 'scripted');
  await press(one(container, {role: 'option', name: 'Idea 1'}));
  await see(container, 'Operator changed checkout');
});

test('a run whose log records no root is still addressed by the catalog, so it shows Pause', async () => {
  shell = await shellOnRun(
    storedWorld(withoutRoot(storeRunFixture('live'))),
    new FakeClock().schedule,
    LIVE,
  );
  one(shell.container, {role: 'button', name: 'Pause', exact: true});
});
