/**
 * Opens the shell on a run fixture: the Fake bridge, a listing that shows the fixture's run live or
 * ended, and the fixture server (`fake-fixture-server.ts`) replaying its events. The one place the
 * journeys and the screen checks get a realistic run from, instead of a hand-made one.
 */
import type {Page} from '@playwright/test';
import {expect} from '@playwright/test';
import type {WelcomeBridge} from '@vibesys/ipc';
import {type EndOutcome, type RunFixture, splitLive} from '@vibesys/run-fixtures';
import {
  type CoreMethods,
  type FakeBridgeSeed,
  installFakeBridge,
  installFakeMachines,
  installFakeNewRun,
  installFakeProjectRuns,
  installFakeWindows,
  type MachinesMethods,
  type NewRunMethods,
  type WindowsMethods,
} from './fake-bridge.js';
import {type FixtureServerMethods, installFakeFixtureServer} from './fake-fixture-server.js';
import {type FakeHistorySeed, installFakeHistory} from './fake-history.js';
import {FAKE_OFFER} from './fake-offer.js';
import {installFakeRun, type RunMethods} from './fake-run.js';
import {type FakeSummarySpanSeed, installFakeSummarySpan} from './fake-summary-span.js';
import {STARTED_AT} from './started-at.js';

const PROJECT = '/work/queue-rs';
const TASK = 'mpmc';
const INSTANCE = '00000000000a';
const HOST = 'ssh:remote-host';
const CHECKOUT = '~/vibesys';

/**
 * Whether the Fake bridge the fixture world installs has every method of the real one: the
 * installers `fixtureScripts` runs, together, provide all of `WelcomeBridge`. A method added to
 * the bridge and not to the Fake makes this a compile error here, not a page that fails when
 * someone first presses the control that calls it.
 */
type ProvidedByWorld =
  | CoreMethods
  | MachinesMethods
  | NewRunMethods
  | WindowsMethods
  | RunMethods
  | FixtureServerMethods;
type MissingMethods = Exclude<keyof WelcomeBridge, ProvidedByWorld>;
const complete: [MissingMethods] extends [never] ? true : MissingMethods = true;
void complete;

interface FixtureWindow extends Window {
  fakeHost: {
    listing(listing: unknown): void;
    toggleSidebar(): void;
    emit(events: readonly unknown[]): void;
    drop(): void;
    releaseHistory(): void;
  };
}

export interface OpenOptions {
  readonly theme?: 'ink' | 'paper';
  /** Whether the sidebar is shown; shown by default. */
  readonly sidebar?: boolean;
  /** A finished run's history served in small pages, held part-way: see `fake-history.ts`. */
  readonly historyServing?: Omit<FakeHistorySeed, 'events'>;
  readonly summaryServing?: Omit<FakeSummarySpanSeed, 'events'>;
}

/** The registry phase a run that ended with `outcome` stays in. */
function phaseOf(outcome: EndOutcome | undefined): string {
  if (outcome === undefined) return 'running';
  if (outcome === 'failed' || outcome === 'adoption-failed') return 'failed';
  if (outcome === 'stopped' || outcome === 'force_stopped') return 'stopped';
  return 'completed';
}

function server(fixture: RunFixture): Record<string, unknown> {
  const end = fixture.end;
  const phase = phaseOf(end?.outcome);
  return {
    id: INSTANCE,
    projectRoot: PROJECT,
    runId: fixture.runId,
    status: phase,
    phase,
    paused: null,
    end: end === undefined ? null : {...end, resumable: false},
    startedAt: STARTED_AT,
    reason: null,
  };
}

/** A listing of the fixture's run, live or ended as the fixture says. */
function listingOf(fixture: RunFixture): Record<string, unknown> {
  const run = server(fixture);
  const live = fixture.state === 'live';
  return {
    kind: 'listed',
    host: HOST,
    live: live ? [run] : [],
    ended: live ? [] : [run],
    unreadable: [],
    unverified: [],
  };
}

/**
 * The runs `host` lists for `PROJECT`: those its servers and registry hold, and those the user
 * opened (Recent), as `runs.list` would answer. The one place the world says which runs a machine
 * has, so the sidebar, All runs and the machines agree.
 */
function projectRuns(
  listing: Record<string, unknown>,
  recent: readonly Record<string, unknown>[],
): Record<string, readonly unknown[]> {
  const servers = [...(listing['live'] as unknown[]), ...(listing['ended'] as unknown[])];
  const runs = new Map<string, unknown>();
  for (const each of servers) {
    const {runId, startedAt} = each as {runId: string; startedAt: number};
    runs.set(runId, {runId, task: TASK, createdAt: startedAt * 1000});
  }
  for (const each of recent) {
    const {runId, task, attachedAt} = each as {runId: string; task: string; attachedAt: number};
    if (!runs.has(runId)) runs.set(runId, {runId, task, createdAt: attachedAt});
  }
  return {[`${HOST}|${PROJECT}`]: [...runs.values()]};
}

function seedOf(fixture: RunFixture, theme: 'ink' | 'paper'): FakeBridgeSeed {
  const listing = listingOf(fixture);
  const recent = [
    {
      host: HOST,
      hostLabel: 'remote-host',
      project: PROJECT,
      task: TASK,
      runId: fixture.runId,
      instanceId: fixture.state === 'live' ? INSTANCE : null,
      attachedAt: 3,
    },
  ];
  return {
    theme,
    chromeInset: {width: 84, height: 40},
    appearance: {mode: theme === 'ink' ? 'dark' : 'light', light: 'paper', dark: 'ink'},
    listings: [listing],
    recent,
    // The machines are the ones the run is on: This Mac, and its host with the run's checkout.
    machines: [
      {host: 'local', label: 'This Mac', checkout: CHECKOUT},
      {host: HOST, label: 'remote-host', checkout: CHECKOUT},
    ],
    projectRuns: projectRuns(listing, recent),
    newRun: {...FAKE_OFFER, projects: {[PROJECT]: [TASK]}},
  };
}

/** A self-contained installer called with its seed, as a script a page runs. */
function script<A>(install: (seed: A) => void, seed: A): string {
  return `(${install.toString()})(${JSON.stringify(seed)});`;
}

/**
 * The scripts that make a page the app opened on `fixture`, in the order they must run before the
 * page's own scripts: the Fake bridge, its machines, the fake run, and the fixture server
 * replaying the run. Playwright adds them as init scripts (`openFixture`); the fixture dev app
 * serves them to the page (`e2e/tools/dev-fixture.ts`).
 */
export function fixtureScripts(fixture: RunFixture, options: OpenOptions = {}): readonly string[] {
  const seed = seedOf(fixture, options.theme ?? 'ink');
  const live = fixture.state === 'live';
  const scripts = [
    script(installFakeBridge, seed),
    script(installFakeMachines, seed),
    script(installFakeProjectRuns, seed),
    script(installFakeNewRun, seed),
    script(installFakeWindows, undefined),
    script(installFakeRun, {streams: [], text: {}}),
    script(installFakeFixtureServer, {
      runId: fixture.runId,
      first: live ? splitLive(fixture).first : fixture.events,
      ...(live ? {} : {history: fixture.events}),
      overview: {
        objective: {
          text: fixture.objective.text,
          metric: fixture.objective.metric,
          unit: fixture.objective.unit,
          direction: fixture.objective.direction,
          correctness_check: [],
        },
      },
    }),
  ];
  if (options.historyServing !== undefined) {
    scripts.push(script(installFakeHistory, {...options.historyServing, events: fixture.events}));
  }
  if (options.summaryServing !== undefined) {
    scripts.push(
      script(installFakeSummarySpan, {...options.summaryServing, events: fixture.events}),
    );
  }
  return scripts;
}

/** Opens the app on `fixture` and its run: the first batch is in, the rest waits for `playLive`. */
export async function openFixture(
  page: Page,
  fixture: RunFixture,
  options: OpenOptions = {},
): Promise<void> {
  for (const content of fixtureScripts(fixture, options)) await page.addInitScript({content});
  await page.goto('./shell.html');
  await page
    .getByRole('navigation', {name: 'VibeSys'})
    .locator('button.vs-sidebar-link[title*="queue-rs · mpmc"]')
    .click();
  if (options.sidebar === false) {
    await page.evaluate(() => (window as unknown as FixtureWindow).fakeHost.toggleSidebar());
    await expect(page.getByRole('navigation', {name: 'VibeSys'})).toHaveCount(0);
  }
  await expect(page.getByRole('main')).toBeVisible();
}

/** Streams the fixture's live events, drops the stream if it says so, then streams the rest. */
export async function playLive(page: Page, fixture: RunFixture): Promise<void> {
  const {beforeDrop, afterDrop} = splitLive(fixture);
  await page.evaluate(
    events => (window as unknown as FixtureWindow).fakeHost.emit(events),
    beforeDrop,
  );
  if (fixture.dropAfter === undefined) return;
  await page.evaluate(() => (window as unknown as FixtureWindow).fakeHost.drop());
  await page.evaluate(
    events => (window as unknown as FixtureWindow).fakeHost.emit(events),
    afterDrop,
  );
}

/** Lets a held history read finish, so the run's screen settles. */
export async function releaseHistory(page: Page): Promise<void> {
  await page.evaluate(() => (window as unknown as FixtureWindow).fakeHost.releaseHistory());
}
