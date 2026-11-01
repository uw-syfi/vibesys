/**
 * The rail: always there, beside every screen, and marking where the user is. Drawn in a DOM over
 * the world of the browser journeys; the browser keeps where the rail sits and what the window's
 * controls cover (`e2e/rail.spec.ts`).
 */
import {afterAll, afterEach, beforeAll, expect, test} from 'bun:test';
import {FakeClock} from '@vibesys/transport/testing';
import {Window} from 'happy-dom';
import {press} from './testing/dom-controls.js';
import {all, one} from './testing/dom-query.js';
import {SEED} from './testing/seed.js';
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
  for (const name of [...installed, 'window', 'IS_REACT_ACT_ENVIRONMENT'])
    Reflect.deleteProperty(globalThis, name);
  await window.happyDOM.close();
});

const shells: OpenShell[] = [];
afterEach(async () => {
  for (const shell of shells.splice(0)) await shell.close();
});

const DESTINATIONS = ['New', 'Runs', 'Machines', 'Usage', 'Settings'] as const;

async function launch(): Promise<OpenShell> {
  const shell = await openShell(null, {schedule: new FakeClock().schedule, seed: SEED});
  shells.push(shell);
  return shell;
}

const rail = (shell: OpenShell): HTMLElement =>
  one(shell.container, {role: 'navigation', name: 'VibeSys'});

test('the rail is the one navigation: the mark, New, Runs, Machines, Usage, and Settings below', async () => {
  const shell = await launch();
  const names = all(rail(shell), {role: 'button'}).map(
    button => button.getAttribute('aria-label') ?? button.textContent?.trim() ?? '',
  );
  expect(names).toEqual(['VibeSys', 'New', 'Runs', 'Machines', 'Usage', 'Settings']);
  expect(all(shell.container, {role: 'navigation', name: 'VibeSys'})).toHaveLength(1);
});

test('the rail marks where the user is, on every screen', async () => {
  const shell = await launch();
  for (const name of DESTINATIONS) {
    await press(one(rail(shell), {role: 'button', name, exact: true}));
    await shell.read();
    expect(one(rail(shell), {role: 'button', name, exact: true}).getAttribute('aria-current')).toBe(
      'page',
    );
    expect(rail(shell).querySelectorAll('[aria-current="page"]')).toHaveLength(1);
  }
});

test('the rail starts with the app logo, whose tooltip is the version, and it goes home', async () => {
  const shell = await launch();
  const mark = one(rail(shell), {role: 'button', name: 'VibeSys', exact: true});
  expect(mark.getAttribute('title')).toMatch(/^VibeSys \S+$/);
  expect(mark.querySelectorAll('svg.vs-icon path')).toHaveLength(1);
  await press(one(rail(shell), {role: 'button', name: 'New', exact: true}));
  await shell.read();
  await press(mark);
  await shell.read();
  one(shell.container, {role: 'heading', name: 'Runs'});
});
