/**
 * The layout of the Run screen's controls and the rows of their requests, app window only: Pause
 * pending and refused, applied and parked, guidance, the controls through pause, resume and finish,
 * and while the history still loads, each on one page resized across the widths. What a press posts,
 * what a row says and how the controls follow the journal is `src/run-intents.dom.test.tsx`.
 * `SCREENS_DIR` writes each shot.
 */
import {mkdirSync, writeFileSync} from 'node:fs';
import {join} from 'node:path';
import {create} from '@bufbuild/protobuf';
import {expect, type Page, test} from '@playwright/test';
import {
  IntentAction,
  IntentStep,
  Outcome,
  RecordSchema,
  RunLiveness,
  type Record as StoreRecord,
} from '@vibesys/proto';
import {fixtureNamed, INTENT_REPLAYS} from '@vibesys/run-fixtures';
import {
  finishOpenSpans,
  intentsWorld,
  observationOf,
  refusedResumeWorld,
  tailOf,
} from '../src/testing/intents-world.js';
import {contactSheetHtml, type SheetEntry} from './checks/contact-sheet.js';
import {screenCheck} from './checks/screen-check.js';
import {type Rest, settled} from './checks/settle.js';
import {installGateway, wire} from './fake-gateway.js';
import {openFixture} from './fixture-world.js';

interface GatewayWindow {
  fakeGateway: {append(bytes: number[]): void; observe(bytes: number[]): void};
}

async function openIntents(page: Page, options: {amendment?: boolean; history?: 'pending'} = {}) {
  const world = intentsWorld(options);
  await page.exposeFunction('postIntent', (host: string, bytes: number[]) =>
    world.postIntent(host, bytes),
  );
  await installGateway(page, world.input);
  await openFixture(page, {...fixtureNamed('queue-rs-live'), runId: 'run-a'});
  if (options.history !== 'pending')
    await expect(page.getByRole('button', {name: 'Guidance', exact: true})).toBeVisible();
  const append = (bytes: number[]) =>
    page.evaluate(bytes => (window as unknown as GatewayWindow).fakeGateway.append(bytes), bytes);
  return {
    world,
    outcome: (index: number, result: 'applied' | 'refused') => append(world.outcome(index, result)),
    append: (records: StoreRecord[]) => append(tailOf(records)),
    observe: (epoch: number, liveness: RunLiveness) =>
      page.evaluate(
        bytes => (window as unknown as GatewayWindow).fakeGateway.observe(bytes),
        observationOf(epoch, liveness),
      ),
  };
}

const WIDTHS = [1000, 1440, 1920];
const shots = process.env['SCREENS_DIR'];
const sheet: SheetEntry[] = [];
/** `rest` is `'as shown'` for a state that is the point of the spec: a history still loading. */
async function capture(page: Page, name: string, rest: Rest = 'read'): Promise<void> {
  await settled(page, rest);
  expect(await screenCheck(page)).toEqual([]);
  if (!shots) return;
  mkdirSync(shots, {recursive: true});
  const file = `${name}.png`;
  await page.screenshot({path: join(shots, file)});
  sheet.push({name, file});
}

/** Lays the screen the page shows out at every one of `widths`, on the one page. */
async function captureAtEvery(
  page: Page,
  name: string,
  widths: readonly number[] = WIDTHS,
): Promise<void> {
  for (const width of widths) {
    await page.setViewportSize({width, height: 900});
    await capture(page, `${name}-${width}`);
  }
}

test('an addressed Pause lays out at 800 and 1440', async ({page}) => {
  await page.setViewportSize({width: 800, height: 900});
  await openIntents(page, {amendment: true});
  await page.getByRole('button', {name: 'Pause', exact: true}).click();
  await expect(page.getByText('Pause · Pending', {exact: true})).toBeVisible();
  await captureAtEvery(page, 'amendment-pause', [800, 1440]);
});

for (const kind of INTENT_REPLAYS) {
  test(`intent ${kind} lays out at every width`, async ({page}) => {
    await page.setViewportSize({width: WIDTHS[0] ?? 1000, height: 900});
    const {outcome} = await openIntents(page);
    await page.getByRole('button', {name: 'Pause', exact: true}).click();
    await expect(page.getByText('Pause · Pending', {exact: true})).toBeVisible();
    if (kind !== 'pending') {
      await outcome(0, kind === 'park-applied' ? 'applied' : kind);
      if (kind === 'refused')
        await expect(page.getByText('Pause · Refused', {exact: true})).toBeVisible();
      else await expect(page.getByText('Pausing', {exact: true}).first()).toBeVisible();
    }
    await captureAtEvery(page, `intent-${kind}`);
  });
}

test('guidance beside the controls lays out', async ({page}) => {
  await page.setViewportSize({width: 1000, height: 900});
  await openIntents(page);
  await page.getByRole('button', {name: 'Guidance', exact: true}).click();
  const dialog = page.getByRole('dialog', {name: 'Guidance', exact: true});
  await expect(dialog).toBeVisible();
  await dialog.getByRole('textbox', {name: 'Guidance', exact: true}).fill('Measure contention');
  await capture(page, 'intent-guidance-1000');
});

test('live controls stay reachable and lay out through pause, resume and finish', async ({
  page,
}) => {
  await page.setViewportSize({width: 800, height: 900});
  const {world, outcome, append, observe} = await openIntents(page);
  const pauseNow = page.getByRole('button', {name: 'Pause now', exact: true});
  await expect(pauseNow).toBeEnabled();
  await expect(pauseNow).toBeInViewport();
  await capture(page, 'controls-live-v2');
  await page.getByRole('button', {name: 'Pause', exact: true}).click();
  await expect(page.getByText('Pause · Pending', {exact: true})).toBeVisible();
  await outcome(0, 'applied');
  await expect(page.getByText('Pausing', {exact: true}).first()).toBeVisible();
  await capture(page, 'controls-draining');
  await pauseNow.click();
  await expect(page.getByText('Pause now · Pending', {exact: true})).toBeVisible();
  await outcome(1, 'applied');
  await observe(1, RunLiveness.ENDED);
  const resume = page.getByRole('button', {name: 'Resume', exact: true});
  await expect(resume).toBeEnabled();
  await expect(page.getByText('Paused', {exact: true}).first()).toBeVisible();
  await capture(page, 'controls-parked');
  await resume.click();
  const header = world.fixture.records.find(record => record.body.case === 'epochStarted');
  if (header?.body.case !== 'epochStarted') throw new Error('epoch header required');
  await append([
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
  ]);
  await observe(2, RunLiveness.ALIVE);
  await expect(pauseNow).toBeEnabled();
  await capture(page, 'controls-resumed');
  await append([
    ...finishOpenSpans(world.fixture.records, world.next),
    create(RecordSchema, {
      seq: world.next(),
      epoch: 2,
      body: {
        case: 'lifecycle',
        value: {state: {case: 'ended', value: {outcome: Outcome.FINISHED, resumable: false}}},
      },
    }),
  ]);
  await observe(2, RunLiveness.ENDED);
  await expect(resume).toBeVisible();
  await expect(pauseNow).toHaveCount(0);
  await capture(page, 'controls-finished');
});

test('Pause now stays reachable and lays out while a live run is still loading history', async ({
  page,
}) => {
  await page.setViewportSize({width: 800, height: 900});
  await openIntents(page, {history: 'pending'});
  await expect(page.getByText(/^History \d+ of \d+$/)).toBeVisible();
  const pauseNow = page.getByRole('button', {name: 'Pause now', exact: true});
  await expect(pauseNow).toBeEnabled();
  await expect(pauseNow).toBeInViewport();
  await capture(page, 'controls-loading-v2', 'as shown');
});

test('intent controls app-window contact sheet', async ({page}) => {
  if (!shots || sheet.length === 0) return;
  writeFileSync(join(shots, 'intents-contact-sheet.html'), contactSheetHtml(sheet, 3));
  await page.goto(`file://${join(shots, 'intents-contact-sheet.html')}`);
  await page.setViewportSize({width: 1500, height: 900});
  await page.screenshot({path: join(shots, 'intents-contact-sheet.png'), fullPage: true});
});

test('a refused Resume row on a catalog-addressed historical run lays out', async ({page}) => {
  const world = refusedResumeWorld();
  await page.exposeFunction('postIntent', (host: string, bytes: number[]) =>
    world.postIntent(host, bytes),
  );
  await installGateway(page, wire(world.fixture));
  await openFixture(page, {...fixtureNamed('finished-run'), runId: 'run-a'});
  await page.getByRole('button', {name: 'Resume', exact: true}).click();
  await expect(page.getByText('Resume · Refused: checkout missing', {exact: true})).toBeVisible();
  await settled(page);
  expect(await screenCheck(page)).toEqual([]);
});
