/**
 * The app never changes screen by itself. It opens on Runs, which shows each machine's steps while
 * the machines answer and says there is no run when they hold none, with a New run action. Runs is
 * always in the rail. The mark goes home, which is New run while no run is known and the Runs
 * dashboard once a machine lists one. Drawn in a DOM over the world of the browser journeys; these
 * held no layout, so they ran in Chromium for nothing.
 */
import {afterAll, afterEach, beforeAll, expect, test} from 'bun:test';
import {FakeClock} from '@vibesys/transport/testing';
import {Window} from 'happy-dom';
import {act} from 'react';
import {press} from './testing/dom-controls.js';
import {all, maybe, notSee, one, see, settle, textOf, until} from './testing/dom-query.js';
import type {FakeBridgeSeed} from './testing/fake-bridge.js';
import {listed, SEED, server} from './testing/seed.js';
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

interface FakeHost {
  listing(listing: unknown): void;
  readonly retried: readonly string[];
}
const fakeHost = (): FakeHost => (globalThis.window as unknown as {fakeHost: FakeHost}).fakeHost;

const NO_RUNS: FakeBridgeSeed = {
  ...SEED,
  listings: [
    {kind: 'listed', host: 'local', live: [], ended: [], unreadable: [], unverified: []},
    listed([], []),
  ],
  recent: [],
  projectRuns: {},
};

/** The app as it launches over `seed`: on the screen it decides on. */
async function launch(seed: FakeBridgeSeed = SEED): Promise<OpenShell> {
  const shell = await openShell(null, {schedule: new FakeClock().schedule, seed});
  shells.push(shell);
  return shell;
}

const rail = (shell: OpenShell): HTMLElement =>
  one(shell.container, {role: 'navigation', name: 'VibeSys'});
const railButton = (shell: OpenShell, name: string): HTMLElement =>
  one(rail(shell), {role: 'button', name, exact: true});
const runsHeading = (shell: OpenShell): HTMLElement | null =>
  maybe(shell.container, {role: 'heading', name: 'Runs', level: 1});

async function showScreen(shell: OpenShell, destination: string): Promise<void> {
  await press(railButton(shell, destination));
  await shell.read();
}

/** Presses the app's mark, which goes home, once the screen has read what it shows. */
async function pressMark(shell: OpenShell): Promise<void> {
  await shell.read();
  await press(railButton(shell, 'VibeSys'));
  await shell.read();
}

/** New run's first page names no run list: it is the form, with the rail marking New. */
function expectNewRun(shell: OpenShell): void {
  expect(railButton(shell, 'New').getAttribute('aria-current')).toBe('page');
  expect(runsHeading(shell)).toBeNull();
}

const retried = (): readonly string[] => fakeHost().retried;

async function listingArrives(shell: OpenShell, listing: unknown): Promise<void> {
  await act(async () => fakeHost().listing(listing));
  await settle();
  await shell.read();
}

test('with no run on any machine the app opens on Runs, which says so and offers New run', async () => {
  const shell = await launch(NO_RUNS);
  expect(runsHeading(shell)).not.toBeNull();
  await see(shell.container, 'No runs', true);
  railButton(shell, 'Machines');
  await press(one(shell.container, {role: 'button', name: 'New run', exact: true}));
  await shell.read();
  expectNewRun(shell);
  railButton(shell, 'Runs');
});

test('the mark goes to New run while there is no run, from any screen', async () => {
  const shell = await launch(NO_RUNS);
  await showScreen(shell, 'Usage');
  expect(railButton(shell, 'Usage').getAttribute('aria-current')).toBe('page');
  await pressMark(shell);
  expectNewRun(shell);
});

test('a run appearing sends the mark to the dashboard, without leaving New run', async () => {
  const shell = await launch(NO_RUNS);
  await pressMark(shell);
  expectNewRun(shell);
  await listingArrives(shell, listed([server('00000000000a', '20261015-163047', 'running')], []));
  // The user is not taken from New run by it.
  expectNewRun(shell);
  await showScreen(shell, 'Usage');
  await pressMark(shell);
  expect(runsHeading(shell)).not.toBeNull();
  expect(railButton(shell, 'Runs').getAttribute('aria-current')).toBe('page');
});

test('with a run listed the app opens on the Runs dashboard', async () => {
  const shell = await launch();
  railButton(shell, 'Runs');
  expect(runsHeading(shell)).not.toBeNull();
});

test('a configured machine with a live run lists it on a fresh profile with no recent run', async () => {
  const shell = await launch({...SEED, recent: [], projectRuns: {}});
  railButton(shell, 'Runs');
  expect(runsHeading(shell)).not.toBeNull();
});

test('a machine still connecting keeps the mark on Runs until it answers', async () => {
  const shell = await launch({
    ...NO_RUNS,
    machines: [
      {host: 'local', label: 'This Mac', checkout: '/bundled'},
      {host: 'ssh:host-a', label: 'host-a', checkout: '/checkout'},
    ],
    listings: [
      {kind: 'listed', host: 'local', live: [], ended: [], unreadable: [], unverified: []},
      {
        kind: 'unavailable',
        host: 'ssh:host-a',
        step: 'handshake',
        connection: {kind: 'connecting'},
      },
    ],
  });
  await pressMark(shell);
  expect(runsHeading(shell)).not.toBeNull();
  await listingArrives(shell, {
    kind: 'listed',
    host: 'ssh:host-a',
    live: [],
    ended: [],
    unreadable: [],
    unverified: [],
  });
  await pressMark(shell);
  expectNewRun(shell);
});

const stepLists = (shell: OpenShell): Element[] => [
  ...shell.container.querySelectorAll('.vs-step-list'),
];

test('with runs shown a stopped machine keeps a line with its reason and a Retry', async () => {
  const shell = await launch({
    ...SEED,
    machines: [...(SEED.machines ?? []), {host: 'ssh:host-a', label: 'host-a', checkout: '/c'}],
    listings: [
      ...(SEED.listings ?? []),
      {
        kind: 'unavailable',
        host: 'ssh:host-a',
        step: 'gateway',
        connection: {kind: 'offline', detail: 'gateway startup exited: 1'},
      },
    ],
  });
  expect(runsHeading(shell)).not.toBeNull();
  expect(stepLists(shell)).toHaveLength(0);
  const line = (): Element => {
    const found = [...shell.container.querySelectorAll('*')].find(
      each => each.children.length === 0 && (each.textContent ?? '').trim() === 'host-a',
    );
    if (found?.parentElement == null) throw new Error('no line for host-a');
    return found.parentElement;
  };
  expect(textOf(line())).toContain('gateway exited (code 1)');
  one(line(), {role: 'img', name: 'Failed'});
  await press(one(line(), {role: 'button', name: 'Retry'}));
  await until('the retry asked', () => retried().length > 0);
  expect(retried()).toEqual(['ssh:host-a']);
});

test('a machine that has not answered shows its steps, and a stopped one can be retried', async () => {
  const machines = [
    {host: 'local', label: 'This Mac', checkout: '/bundled'},
    {host: 'ssh:host-a', label: 'host-a', checkout: '/checkout'},
  ];
  const shell = await launch({
    ...NO_RUNS,
    machines,
    listings: [
      {kind: 'unavailable', host: 'local', step: 'handshake', connection: {kind: 'connecting'}},
      {
        kind: 'unavailable',
        host: 'ssh:host-a',
        step: 'gateway',
        connection: {kind: 'offline', detail: 'gateway startup exited: 1'},
      },
    ],
  });
  const listFor = (text: string): Element => {
    const found = stepLists(shell).filter(each => textOf(each).includes(text));
    if (found.length !== 1) throw new Error(`${found.length} step lists hold ${text}`);
    return found[0] as Element;
  };
  const local = listFor('This Mac');
  const items = all(local, {role: 'listitem'}).map(each => textOf(each));
  expect(items).toHaveLength(3);
  expect(items[0]).toMatch(/Starting gateway/);
  expect(items[1]).toMatch(/Connecting.*2\/3/);
  expect(items[2]).toMatch(/Loading runs/);
  expect(all(local, {role: 'button', name: 'Retry'})).toHaveLength(0);
  expect(railButton(shell, 'Runs').getAttribute('aria-current')).toBe('page');

  const remote = listFor('host-a');
  one(remote, {role: 'img', name: 'Failed'});
  expect(textOf(remote)).toContain('gateway exited (code 1)');
  await press(one(remote, {role: 'button', name: 'Retry'}));
  await until('the retry asked', () => retried().length > 0);
  expect(retried()).toEqual(['ssh:host-a']);

  await act(async () => {
    for (const {host} of machines) {
      fakeHost().listing({
        kind: 'listed',
        host,
        live: [],
        ended: [],
        unreadable: [],
        unverified: [],
      });
    }
  });
  await settle();
  await shell.read();
  await see(shell.container, 'No runs', true);
  expect(stepLists(shell)).toHaveLength(0);
  notSee(shell.container, 'Starting gateway');
});
