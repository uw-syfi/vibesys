/**
 * The Runs dashboard's journeys over the world of the browser journeys: every run of every machine
 * as a tile, grouped and regrouped, the runs that need the user in their own row and on the rail's
 * badge, archiving a run, and a machine that cannot be reached or listed. Drawn in a DOM; these
 * held no layout, so they ran in Chromium for nothing. The browser keeps how tiles sit (density,
 * overlap) and the two walks that open a run from a tile.
 */
import {afterAll, afterEach, beforeAll, expect, test} from 'bun:test';
import {FakeClock} from '@vibesys/transport/testing';
import {Window} from 'happy-dom';
import {act} from 'react';
import {
  dashboardWorld,
  listingOf,
  MANY_RUNS,
  NOW,
  OMITTED_RUN,
  ONE_RUN,
  type RunCase,
} from './testing/dashboard-world.js';
import {press} from './testing/dom-controls.js';
import {
  all,
  maybe,
  nameOfControl,
  one,
  pickValue,
  see,
  settle,
  textOf,
  until,
} from './testing/dom-query.js';
import {type FakeBridgeSeed, installFakeDigests} from './testing/fake-bridge.js';
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
let clock = new FakeClock();
afterEach(async () => {
  for (const shell of shells.splice(0)) await shell.close();
});

/** The dashboard on `world`, once it has read what it shows. */
async function open(world: FakeBridgeSeed): Promise<OpenShell> {
  clock = new FakeClock();
  const shell = await openShell(
    {kind: 'runs'},
    {schedule: clock.schedule, seed: world, installs: [installFakeDigests], now: NOW},
  );
  shells.push(shell);
  return shell;
}

const group = (shell: OpenShell, name: string): HTMLElement | null =>
  maybe(shell.container, {role: 'region', name, exact: true});
const tiles = (shell: OpenShell, name: string): Element[] => {
  const found = group(shell, name);
  return found === null ? [] : [...found.querySelectorAll('.vs-overview-tile')];
};
const holding = (tile: readonly Element[], text: string): Element[] =>
  tile.filter(each => textOf(each).includes(text));

function groupTitles(shell: OpenShell): string[] {
  return [...shell.container.querySelectorAll('.vs-overview-group-head')].map(head =>
    [...head.children]
      .map(child => child.textContent ?? '')
      .join(' ')
      .replace(/\s+/g, ' ')
      .trim(),
  );
}

const rail = (shell: OpenShell): HTMLElement =>
  one(shell.container, {role: 'navigation', name: 'VibeSys'});
const railButton = (shell: OpenShell, name: string | RegExp): HTMLElement =>
  one(rail(shell), {role: 'button', name});

async function showScreen(shell: OpenShell, destination: string): Promise<void> {
  await press(one(rail(shell), {role: 'button', name: destination, exact: destination === 'New'}));
  await shell.read();
}

const first = (found: readonly Element[], what: string): Element => {
  const tile = found[0];
  if (tile === undefined) throw new Error(`no ${what}`);
  return tile;
};

test('groups every run of every machine by status, the runs that need the user first', async () => {
  const shell = await open(dashboardWorld(MANY_RUNS));
  expect(groupTitles(shell)).toEqual([
    'Needs you 2',
    'Running 2',
    'Paused 1',
    'Finished 3',
    'Stopped 1',
  ]);
  expect(tiles(shell, 'Needs you')).toHaveLength(2);
  const stalled = holding(tiles(shell, 'Needs you'), 'mpsc');
  expect(stalled).toHaveLength(1);
  expect(textOf(first(stalled, 'stalled run'))).toContain('host-b');
  expect(textOf(first(stalled, 'stalled run'))).toContain('stalled 32m');
  expect(textOf(first(holding(tiles(shell, 'Needs you'), 'spsc'), 'failed run'))).toContain(
    'failed',
  );
  // A run that needs the user is in that group alone.
  expect(shell.container.querySelectorAll('.vs-overview-tile')).toHaveLength(9);
});

test('a tile says its result against the base, where it ran, its ideas and time, and charts every idea', async () => {
  const shell = await open(dashboardWorld(ONE_RUN));
  const tile = first(tiles(shell, 'Running'), 'running tile');
  for (const text of ['queue-rs-run1', 'mpmc', '1.42×', 'host-a', '7 ideas · 40m', 'running']) {
    expect(textOf(tile), text).toContain(text);
  }
  expect(tile.querySelectorAll('.vs-result-chart-mark')).toHaveLength(7);
  expect(tile.querySelectorAll('[data-mark="kept"]')).toHaveLength(3);
  expect(tile.querySelectorAll('[data-mark="not-better"]')).toHaveLength(2);
  expect(tile.querySelectorAll('[data-mark="failed"]')).toHaveLength(1);
  expect(tile.querySelectorAll('[data-mark="pending"]')).toHaveLength(1);
  expect(tile.querySelectorAll('.vs-result-chart-base')).toHaveLength(1);
});

test('a live run whose input was never measured headlines its best measured value', async () => {
  const run: RunCase = {
    ...(ONE_RUN[0] as RunCase),
    points: [
      [null, 'kept'],
      [null, 'running'],
    ],
    best: {value: 287.4, metric: 'output_tokens_per_s', unit: null},
  };
  const shell = await open(dashboardWorld([run]));
  const tile = first(tiles(shell, 'Running'), 'running tile');
  expect(textOf(tile)).toContain('287');
  expect(textOf(tile)).toContain('output tokens/s');
  expect(textOf(tile)).not.toContain('×');
});

test('a run that needs the user says why in a few words, whole words, with the full text on hover', async () => {
  const full = 'NestedRepositoryError: before the run could begin it found a repository inside';
  const long = 'the agent was interrupted because the machine went away during the second round';
  const failing = (runId: string, name: string, endSummary: string): RunCase => ({
    runId,
    name,
    host: 'ssh:host-a',
    task: 'mpmc',
    phase: 'failed',
    ago: 3,
    attention: 'failed',
    endSummary,
    points: [[null, 'failed']],
  });
  const shell = await open(
    dashboardWorld([
      failing('r-fail-01', 'queue-rs-a', full),
      failing('r-fail-02', 'queue-rs-b', long),
    ]),
  );
  const notes = tiles(shell, 'Needs you').flatMap(tile => [
    ...tile.querySelectorAll('.vs-overview-tile-note'),
  ]);
  const shown = notes.map(note => ({
    text: note.textContent ?? '',
    title: note.getAttribute('title') ?? '',
  }));
  expect(shown.map(note => note.text).sort()).toEqual([
    'Nested repository error',
    'the agent was interrupted because…',
  ]);
  expect(shown.map(note => note.title).sort()).toEqual([full, long].sort());
});

test('a run with more ideas than its chart keeps still counts them all', async () => {
  const shell = await open(dashboardWorld(OMITTED_RUN));
  const tile = first(tiles(shell, 'Running'), 'running tile');
  expect(textOf(tile)).toContain('100 ideas');
  expect(tile.querySelectorAll('.vs-result-chart-mark')).toHaveLength(64);
});

test('Group by regroups the same tiles by machine and by status', async () => {
  const shell = await open(dashboardWorld(MANY_RUNS));
  const by = () => one(shell.container, {role: 'combobox', name: 'Group by'}) as HTMLSelectElement;
  expect(all(by(), {role: 'option'}).map(option => textOf(option))).toEqual(['Status', 'Machine']);
  const names = (): string[] => groupTitles(shell).filter(title => !title.startsWith('Needs you'));

  await pickValue(by(), 'machine');
  expect(names()).toEqual(['host-a 4', 'host-b 3']);
  await pickValue(by(), 'status');
  expect(names()[0]).toBe('Running 2');
  // Needs you is its own group whatever the grouping.
  expect(tiles(shell, 'Needs you')).toHaveLength(2);
});

test('the rail counts the runs that need the user, and Runs and the mark both go home', async () => {
  const shell = await open(dashboardWorld(MANY_RUNS));
  expect(nameOfControl(railButton(shell, /^Runs/))).toMatch(/2 need you/);
  await showScreen(shell, 'New');
  expect(all(shell.container, {role: 'heading', name: 'Runs'})).toHaveLength(0);
  await shell.read();
  await press(one(rail(shell), {role: 'button', name: 'VibeSys', exact: true}));
  await shell.read();
  one(shell.container, {role: 'heading', name: 'Runs'});
  expect(railButton(shell, /^Runs/).getAttribute('aria-current')).toBe('page');
});

test('Archive takes a run off the dashboard', async () => {
  const shell = await open(dashboardWorld(MANY_RUNS));
  expect(tiles(shell, 'Stopped')).toHaveLength(1);
  await press(one(shell.container, {role: 'button', name: 'More for queue-rs-run9'}));
  await settle();
  await press(one(shell.container, {role: 'menuitem', name: 'Archive'}));
  await settle();
  await shell.read();
  expect(group(shell, 'Stopped')).toBeNull();
});

test('a machine that cannot list a project’s runs still shows the runs its registry reports', async () => {
  const world = dashboardWorld(MANY_RUNS);
  const rest = Object.fromEntries(
    Object.entries(world.projectRuns ?? {}).filter(([key]) => !key.startsWith('ssh:host-b|')),
  );
  const shell = await open({...world, projectRuns: rest});
  // The runs the machine itself reports (live and recently ended) need no project listing.
  const every = [...shell.container.querySelectorAll('.vs-overview-tile')];
  expect(holding(every, 'host-b')).toHaveLength(4);
  expect(holding(every, 'host-a')).toHaveLength(5);
});

test('a host that sends no chart data still shows each run as a tile without a chart', async () => {
  const world = dashboardWorld(ONE_RUN);
  const digests = Object.fromEntries(
    Object.entries(world.digests ?? {}).map(([id, entry]) => {
      const {dashboard: _chart, ...plain} = entry as Record<string, unknown>;
      return [id, plain];
    }),
  );
  const shell = await open({...world, digests});
  const tile = first(tiles(shell, 'Running'), 'running tile');
  expect(textOf(tile)).toContain('mpmc');
  expect(tile.querySelectorAll('.vs-result-chart')).toHaveLength(0);
});

test('Machines carries a warning dot while a machine cannot be reached', async () => {
  const world = dashboardWorld(ONE_RUN as readonly RunCase[]);
  const shell = await open({
    ...world,
    reports: {'ssh:host-b': {kind: 'unavailable', detail: 'down'}},
  });
  await until('the machine to read as unreachable', () =>
    /Unreachable/.test(nameOfControl(railButton(shell, /^Machines/))),
  );
});

test('a run that ends moves from Running to Finished', async () => {
  const shell = await open(dashboardWorld(ONE_RUN));
  expect(tiles(shell, 'Running')).toHaveLength(1);
  const finished = [{...ONE_RUN[0], phase: 'completed'} as RunCase];
  const host = globalThis.window as unknown as {
    fakeRunDigests: {set(value: unknown): void};
    fakeHost: {listing(listing: unknown): void};
  };
  await act(async () => {
    host.fakeRunDigests.set(dashboardWorld(finished).digests);
    host.fakeHost.listing(listingOf(finished, 'ssh:host-a'));
  });
  await settle();
  await shell.read();
  expect(group(shell, 'Running')).toBeNull();
  expect(tiles(shell, 'Finished')).toHaveLength(1);
  expect(textOf(first(tiles(shell, 'Finished'), 'finished tile'))).toContain('finished');
});

test('on a machine whose VibeSys is too old for digests, a run still says its own status', async () => {
  const world = dashboardWorld(MANY_RUNS, ['ssh:host-b']);
  const key = Object.keys(world.projectRuns ?? {}).find(each => each.startsWith('ssh:host-b'));
  if (key === undefined) throw new Error('the world lists nothing on host-b');
  // A run the registry no longer holds: only a digest could say how it went.
  const old = {runId: 'r-old-99', name: 'r-old-99', task: 'mpmc', createdAt: NOW - 50 * 3_600_000};
  const shell = await open({
    ...world,
    projectRuns: {...world.projectRuns, [key]: [...(world.projectRuns?.[key] ?? []), old]},
  });
  // host-b's finished runs come from the registry's record of their end, not from a digest.
  const finished = holding(tiles(shell, 'Finished'), 'host-b');
  expect(finished).toHaveLength(2);
  expect(textOf(first(finished, 'finished tile'))).toContain('finished');
  expect(textOf(first(finished, 'finished tile'))).not.toContain('newer VibeSys');
  // A run only a digest could place is unknown, with the machine's trouble beside it.
  const unknown = first(tiles(shell, 'Unknown'), 'unknown tile');
  expect(textOf(unknown)).toContain('unknown');
  expect(textOf(unknown)).toContain('needs an update');
  await see(shell.container, 'Unknown', true);
});
