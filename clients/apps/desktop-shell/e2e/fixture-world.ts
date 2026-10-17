/**
 * Opens the shell on a run fixture: the Fake bridge, a listing that shows the fixture's run live or
 * ended, and the fixture server (`fake-fixture-server.ts`) replaying its events. The one place the
 * journeys and the screen checks get a realistic run from, instead of a hand-made one.
 */
import type {Page} from '@playwright/test';
import {expect} from '@playwright/test';
import {type EndOutcome, type RunFixture, splitLive} from '@vibesys/run-fixtures';
import {type FakeBridgeSeed, installFakeBridge, installFakeMachines} from './fake-bridge.js';
import {installFakeFixtureServer} from './fake-fixture-server.js';
import {installFakeRun} from './fake-run.js';

const PROJECT = '/work/queue-rs';
const INSTANCE = '00000000000a';
const HOST = 'ssh:remote-host';

interface FixtureWindow extends Window {
  fakeHost: {
    listing(listing: unknown): void;
    toggleSidebar(): void;
    emit(events: readonly unknown[]): void;
    drop(): void;
  };
}

export interface OpenOptions {
  readonly theme?: 'ink' | 'paper';
  /** Whether the sidebar is shown; shown by default. */
  readonly sidebar?: boolean;
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
    startedAt: 1,
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

function seedOf(fixture: RunFixture, theme: 'ink' | 'paper'): FakeBridgeSeed {
  return {
    theme,
    chromeInset: {width: 84, height: 40},
    appearance: {mode: theme === 'ink' ? 'dark' : 'light', light: 'paper', dark: 'ink'},
    listings: [listingOf(fixture)],
    recent: [
      {
        host: HOST,
        hostLabel: 'remote-host',
        project: PROJECT,
        task: 'mpmc',
        runId: fixture.runId,
        instanceId: fixture.state === 'live' ? INSTANCE : null,
        attachedAt: 3,
      },
    ],
  };
}

/** Opens the app on `fixture` and its run: the first batch is in, the rest waits for `playLive`. */
export async function openFixture(
  page: Page,
  fixture: RunFixture,
  options: OpenOptions = {},
): Promise<void> {
  const theme = options.theme ?? 'ink';
  const seed = seedOf(fixture, theme);
  const live = fixture.state === 'live';
  await page.addInitScript(installFakeBridge, seed);
  await page.addInitScript(installFakeMachines, seed);
  await page.addInitScript(installFakeRun, {streams: [], text: {}});
  await page.addInitScript(installFakeFixtureServer, {
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
  });
  await page.goto('./shell.html');
  await page
    .getByRole('navigation', {name: 'VibeSys'})
    .locator('button.vs-sidebar-link', {hasText: 'queue-rs · mpmc'})
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
