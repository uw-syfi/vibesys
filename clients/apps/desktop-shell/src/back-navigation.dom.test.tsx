/**
 * Back and Forward over the whole location, as the window's history holds it: a run's zooms, the
 * dashboard before them, and the screens of the rail, each screen one entry however often it is
 * pressed; the Runs crumb going up to the dashboard from any level; a zoom that does not follow
 * the user out of its run. The shell is drawn in a DOM over the journeys' Fake gateway; the mouse's
 * own buttons through the browser's input, a reload at a zoom, a layout that does not move and
 * the scroll Back restores are the browser's (`e2e/back-navigation.spec.ts`).
 */
import {afterAll, afterEach, beforeAll, expect, test} from 'bun:test';
import {FakeClock} from '@vibesys/transport/testing';
import {Window} from 'happy-dom';
import {act} from 'react';
import {press} from './testing/dom-controls.js';
import {all, maybe, one, until} from './testing/dom-query.js';
import {journeyFixture, journeyWorld} from './testing/journey-records.js';
import {openRun, shellOnDashboard} from './testing/run-screen.js';
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

const IDEA = 'Combine W1 ring with W2 padding';

async function opened(): Promise<HTMLElement> {
  shell = await shellOnDashboard(
    journeyWorld('trace'),
    new FakeClock().schedule,
    journeyFixture('trace'),
  );
  return shell.container;
}

const main = (container: Element) => one(container, {role: 'main'});
const onDashboard = (container: Element): boolean =>
  maybe(main(container), {role: 'heading', name: 'Runs'}) !== null;
const ideasShown = (container: Element): boolean =>
  maybe(container, {role: 'listbox', name: 'Ideas'}) !== null;
const ideaShown = (container: Element): boolean =>
  maybe(container, {role: 'heading', name: IDEA}) !== null;
const go = (way: 'back' | 'forward'): Promise<void> => act(async () => shell?.address[way]());

async function openIdea(container: Element): Promise<void> {
  await openRun(shell as OpenShell);
  await press(
    one(one(container, {role: 'listbox', name: 'Ideas'}), {
      role: 'option',
      name: /Combine W1 ring/,
    }),
  );
  await until('the idea', () => ideaShown(container));
}

const rail = (container: Element, name: string) =>
  one(one(container, {role: 'navigation', name: 'VibeSys'}), {
    role: 'button',
    name: new RegExp(`^${name}`),
  });

test('Back retraces a run’s zooms, leaves it for the dashboard, and Forward returns', async () => {
  const container = await opened();
  await openIdea(container);
  await go('back');
  await until('the ideas', () => ideasShown(container) && !ideaShown(container));
  await go('back');
  await until('the dashboard', () => onDashboard(container));
  await go('forward');
  await until('the run', () => ideasShown(container));
  await go('forward');
  await until('the idea', () => ideaShown(container));
});

test('the Runs crumb goes up to the dashboard from any level, and Back returns to that level', async () => {
  const container = await opened();
  await openIdea(container);
  // The row of crumbs, with the way up to the dashboard first, shows from the idea's steps on.
  await press(
    all(one(container, {role: 'listbox', name: /steps$/}), {role: 'option'})[0] as Element,
  );
  const crumbs = () => one(container, {role: 'navigation', name: 'Zoom'});
  await press(one(crumbs(), {role: 'button', name: 'Runs', exact: true}));
  await until('the dashboard', () => onDashboard(container));
  await go('back');
  await until('the crumbs', () => maybe(container, {role: 'navigation', name: 'Zoom'}) !== null);
});

test('Back crosses screens, and each screen keeps one entry', async () => {
  const container = await opened();
  await press(rail(container, 'Machines'));
  await press(rail(container, 'Machines'));
  await press(rail(container, 'Settings'));
  await press(rail(container, 'Settings'));
  await press(one(main(container), {role: 'button', name: 'Back'}));
  const at = () => shell?.address.entries[shell.address.at];
  await until('Machines', () => at() === '#machines' || at() === 'machines');
  await go('back');
  await until('the dashboard', () => onDashboard(container));
  await go('forward');
  await go('forward');
  expect(shell?.address.entries[shell.address.at]).toMatch(/settings$/);
});

test('leaving a zoomed run keeps its zoom from the next run opened', async () => {
  const container = await opened();
  await openIdea(container);
  await press(rail(container, 'Runs'));
  await until('the dashboard', () => onDashboard(container));
  expect(shell?.address.entries[shell.address.at] ?? '').not.toContain('idea=');
  await openRun(shell as OpenShell);
  await until('the run level', () => ideasShown(container));
  expect(ideaShown(container)).toBe(false);
});
