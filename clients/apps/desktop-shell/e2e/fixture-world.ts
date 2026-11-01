/**
 * Opens the shell on a run fixture: the Fake bridge, a listing that shows the fixture's run live or
 * ended, and the fixture server (`fake-fixture-server.ts`) replaying its events. The one place the
 * journeys and the screen checks get a realistic run from, instead of a hand-made one.
 */

import type {Page} from '@playwright/test';
import {expect} from '@playwright/test';
import type {WelcomeBridge} from '@vibesys/ipc';
import {type RunFixture, splitLive} from '@vibesys/run-fixtures';
import {
  type CoreMethods,
  type ExampleDestinationMethods,
  installFakeBridge,
  installFakeChrome,
  installFakeExampleDestination,
  installFakeMachineSettings,
  installFakeMachines,
  installFakeNewRun,
  installFakeProjectRuns,
  installFakeUsage,
  installFakeUsageLimits,
  installFakeUsagePrices,
  installFakeWindows,
  installRequestBound,
  type MachineSettingsMethods,
  type MachinesMethods,
  type NewRunMethods,
  type UsageMethods,
  type WindowsMethods,
} from '../src/testing/fake-bridge.js';
import {seedOf} from '../src/testing/fixture-seed.js';
import {addInitScripts, initScript as script} from './init-scripts.js';
import {type NativeFixtureOptions, nativeFixtureScript} from './native-fixture.js';
import {whenRead} from './read.js';
import {installGatewayOnly, type RetiredRunMethods} from './retired-run-bridge.js';

/** How often a page may ask the bridge for the same thing; far above any screen's own reads. */
const REQUEST_LIMIT = 100;

/**
 * Whether the Fake bridge the fixture world installs has every method of the real one: the
 * installers `fixtureScripts` runs, together, provide all of `WelcomeBridge`. A method added to
 * the bridge and not to the Fake makes this a compile error here, not a page that fails when
 * someone first presses the control that calls it.
 */
type ProvidedByWorld =
  | CoreMethods
  | MachinesMethods
  | MachineSettingsMethods
  | NewRunMethods
  | UsageMethods
  | ExampleDestinationMethods
  | WindowsMethods
  | RetiredRunMethods;
type MissingMethods = Exclude<keyof WelcomeBridge, ProvidedByWorld>;
const complete: [MissingMethods] extends [never] ? true : MissingMethods = true;
void complete;

interface FixtureWindow extends Window {
  fakeGateway: {calls(): readonly string[]};
  fakeHost: {
    listing(listing: unknown): void;
    emit(events: readonly unknown[]): void;
    drop(): void;
    releaseHistory(): void;
    requestLoops?: string[];
  };
}

export interface OpenOptions extends NativeFixtureOptions {
  readonly theme?: 'ink' | 'paper';
}

/**
 * The scripts that make a page the app opened on `fixture`, in the order they must run before the
 * page's own scripts: the Fake bridge, its machines, and the native gateway replay.
 * Playwright adds them as one init script (`openFixture`); the fixture dev app
 * serves them to the page (`e2e/tools/dev-fixture.ts`).
 */
export function fixtureScripts(fixture: RunFixture, options: OpenOptions = {}): readonly string[] {
  const seed = seedOf(fixture, options.theme ?? 'ink');
  const scripts = [
    script(installFakeChrome, seed),
    script(installFakeBridge, seed),
    script(installFakeMachines, seed),
    script(installFakeMachineSettings, seed),
    script(installFakeProjectRuns, seed),
    script(installFakeNewRun, seed),
    script(installFakeUsagePrices, undefined),
    script(installFakeUsage, seed),
    script(installFakeUsageLimits, seed),
    script(installFakeExampleDestination, seed),
    script(installFakeWindows, undefined),
    script(installGatewayOnly, undefined),
  ];
  scripts.push(nativeFixtureScript(fixture, options));
  return scripts;
}

/** Opens the app on `fixture` and its run: the first batch is in, the rest waits for `playLive`. */
export async function openFixture(
  page: Page,
  fixture: RunFixture,
  options: OpenOptions = {},
): Promise<void> {
  await addInitScripts(page, [
    ...fixtureScripts(fixture, options),
    // The dev app stays open; only the test world bounds requests.
    script(installRequestBound, REQUEST_LIMIT),
  ]);
  await page.goto('./shell.html');
  // The dashboard has read the runs it lists before the run is picked from it.
  await whenRead(page);
  await page
    .getByRole('main')
    .getByRole('button', {name: /^queue-rs · mpmc/})
    .first()
    .click();
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
  const subscriptions = () =>
    page.evaluate(() => {
      const calls = (window as unknown as FixtureWindow).fakeGateway.calls();
      return ['Tail'].map(method => calls.filter(call => call.endsWith(`/${method}`)).length);
    });
  const before = await subscriptions();
  await page.evaluate(() => (window as unknown as FixtureWindow).fakeHost.drop());
  await page.evaluate(
    events => (window as unknown as FixtureWindow).fakeHost.emit(events),
    afterDrop,
  );
  // A reconnecting fixture is ready after both real reader subscriptions resume.
  await expect
    .poll(async () => (await subscriptions()).every((count, index) => count > (before[index] ?? 0)))
    .toBe(true);
}

/** Lets a held history read finish, so the run's screen settles. */
export async function releaseHistory(page: Page): Promise<void> {
  await page.evaluate(() =>
    (window as unknown as {fakeGateway: {releaseTail(): void}}).fakeGateway.releaseTail(),
  );
}
