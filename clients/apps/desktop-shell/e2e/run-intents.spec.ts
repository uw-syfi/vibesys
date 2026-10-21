import {mkdirSync, writeFileSync} from 'node:fs';
import {join} from 'node:path';
import {create, fromBinary, toBinary} from '@bufbuild/protobuf';
import {expect, type Page, test} from '@playwright/test';
import {
  HostCallRequestSchema,
  HostCallResponseSchema,
  IntentAction,
  LifecycleSchema,
  type PostIntentRequest,
  RecordSchema,
  TailResponseSchema,
} from '@vibesys/proto';
import {fixtureNamed, INTENT_REPLAYS, intentReplay, storeRunFixture} from '@vibesys/run-fixtures';
import {contactSheetHtml, type SheetEntry} from './checks/contact-sheet.js';
import {screenCheck} from './checks/screen-check.js';
import {settled} from './checks/settle.js';
import {installGateway, wire} from './fake-gateway.js';
import {openFixture} from './fixture-world.js';

interface GatewayWindow {
  fakeGateway: {
    calls(): string[];
    active(): number;
    append(bytes: number[]): void;
    disconnect(): void;
  };
  fakeHost: {calls: unknown[][]; recent(runs: readonly unknown[]): void};
}

function appliedState(action: IntentAction) {
  return create(LifecycleSchema, {
    state:
      action === IntentAction.PARK
        ? {case: 'parked', value: {}}
        : {
            case: 'running',
            value:
              action === IntentAction.PAUSE ? {hold: {reason: {case: 'operator', value: {}}}} : {},
          },
  }).state;
}

async function openIntents(page: Page) {
  const fixture = storeRunFixture('live');
  const requests: {host: string; request: PostIntentRequest}[] = [];
  let sequence = fixture.records.at(-1)?.seq ?? 0n;
  await page.exposeFunction('postIntent', (host: string, bytes: number[]) => {
    const envelope = fromBinary(HostCallRequestSchema, new Uint8Array(bytes));
    if (envelope.method.case !== 'postIntent') throw new Error('Expected PostIntent');
    requests.push({host, request: envelope.method.value});
    const replay = intentReplay('pending', envelope.method.value.requestId);
    return [...toBinary(HostCallResponseSchema, replay.admission)];
  });
  await page.addInitScript(installGateway, wire(fixture));
  await openFixture(page, {...fixtureNamed('queue-rs-live'), runId: 'run-a'});
  const toggle = page.getByRole('switch', {name: 'Run transport v2'});
  await expect(toggle).not.toBeChecked();
  expect(
    await page.evaluate(() => (window as unknown as GatewayWindow).fakeGateway.calls()),
  ).toEqual([]);
  await toggle.check();
  await expect(page.getByRole('button', {name: 'Guidance', exact: true})).toBeVisible();
  const outcome = async (index: number, result: 'applied' | 'refused'): Promise<void> => {
    const request = requests[index]?.request;
    if (!request) throw new Error('No posted intent');
    const action =
      request.intent.case === 'park'
        ? IntentAction.PARK
        : request.intent.case === 'resume'
          ? IntentAction.RESUME
          : IntentAction.PAUSE;
    const {records} = intentReplay(result, request.requestId, ++sequence, action);
    if (result === 'applied') {
      records.push(
        create(RecordSchema, {
          seq: ++sequence,
          epoch: 1,
          at: {seconds: 1700000101n},
          body: {
            case: 'lifecycle',
            value: {
              state: appliedState(action),
            },
          },
        }),
      );
    }
    const bytes = [
      ...toBinary(
        TailResponseSchema,
        create(TailResponseSchema, {
          records: records.map(record => toBinary(RecordSchema, record)),
          throughSeq: sequence,
        }),
      ),
    ];
    await page.evaluate(
      bytes => (window as unknown as GatewayWindow).fakeGateway.append(bytes),
      bytes,
    );
  };
  return {requests, outcome, toggle};
}

const shots = process.env['SCREENS_DIR'];
const sheet: SheetEntry[] = [];
async function capture(page: Page, name: string): Promise<void> {
  await settled(page);
  expect(await screenCheck(page)).toEqual([]);
  if (!shots) return;
  mkdirSync(shots, {recursive: true});
  const file = `${name}.png`;
  await page.screenshot({path: join(shots, file)});
  sheet.push({name, file});
}

test('pause then resume uses the host intent path and waits for journal outcomes', async ({
  page,
}) => {
  const {requests, outcome, toggle} = await openIntents(page);
  await page.getByRole('button', {name: 'Pause', exact: true}).click();
  await expect(page.getByText('Pause · Pending', {exact: true})).toBeVisible();
  expect(requests[0]).toMatchObject({
    host: 'ssh:remote-host',
    request: {
      runId: 'run-a',
      projectRoot: '/work/queue-rs',
      intent: {case: 'pause'},
    },
  });
  await outcome(0, 'applied');
  await expect(page.getByText('Pause · Applied', {exact: true})).toBeVisible();
  await expect(page.getByText('Paused by you', {exact: true}).first()).toBeVisible();
  await page.getByRole('button', {name: 'Resume', exact: true}).click();
  await expect(page.getByText('Resume · Pending', {exact: true})).toBeVisible();
  expect(requests[1]?.request.intent.case).toBe('resume');
  expect(requests[1]?.request.requestId).not.toBe(requests[0]?.request.requestId);
  await outcome(1, 'applied');
  await expect(page.getByText('Resume · Applied', {exact: true})).toBeVisible();
  await expect(page.getByText('Running', {exact: true}).first()).toBeVisible();
  expect(
    await page.evaluate(() =>
      (window as unknown as GatewayWindow).fakeHost.calls.filter(
        call => call[0] === 'resume' || call[0] === 'stopConfirmed',
      ),
    ),
  ).toEqual([]);
  await toggle.uncheck();
  await expect(page.getByRole('button', {name: 'Chat', exact: true})).toBeVisible();
  await expect(page.getByRole('button', {name: 'Guidance', exact: true})).toHaveCount(0);
  expect(await page.evaluate(() => (window as unknown as GatewayWindow).fakeGateway.active())).toBe(
    0,
  );
});

for (const kind of INTENT_REPLAYS) {
  for (const width of [1000, 1440, 1920]) {
    test(`intent ${kind} at ${width}px`, async ({page}) => {
      await page.setViewportSize({width, height: 900});
      const {outcome, requests} = await openIntents(page);
      const action = kind === 'park-applied' ? 'Park' : 'Pause';
      await page.getByRole('button', {name: action, exact: true}).click();
      if (kind !== 'pending') {
        await expect(page.getByText(`${action} · Pending`, {exact: true})).toBeVisible();
        await outcome(0, kind === 'park-applied' ? 'applied' : kind);
      }
      const status = kind === 'pending' ? 'Pending' : kind === 'refused' ? 'Refused' : 'Applied';
      const result = page.getByText(`${action} · ${status}`, {exact: true});
      await expect(result).toBeVisible();
      expect(requests).toHaveLength(1);
      if (kind === 'park-applied') {
        expect(requests[0]?.request.intent.case).toBe('park');
        await expect(page.getByText('Parked', {exact: true}).first()).toBeVisible();
      }
      await capture(page, `intent-${kind}-${width}`);
    });
  }
}

test('guidance opens beside the controls and posts the typed text', async ({page}) => {
  await page.setViewportSize({width: 1000, height: 900});
  const {requests} = await openIntents(page);
  await page.getByRole('button', {name: 'Guidance', exact: true}).click();
  const dialog = page.getByRole('dialog', {name: 'Guidance', exact: true});
  await expect(dialog).toBeVisible();
  await dialog.getByRole('textbox', {name: 'Guidance', exact: true}).fill('Measure contention');
  await capture(page, 'intent-guidance-1000');
  await dialog.getByRole('button', {name: 'Send guidance'}).click();
  await expect(dialog).not.toBeVisible();
  await expect(page.getByText('Guidance · Pending', {exact: true})).toBeVisible();
  expect(requests[0]?.request.intent).toMatchObject({
    case: 'steer',
    value: {text: 'Measure contention'},
  });
});

test('intent controls app-window contact sheet', async ({page}) => {
  if (!shots || sheet.length === 0) return;
  writeFileSync(join(shots, 'intents-contact-sheet.html'), contactSheetHtml(sheet, 3));
  await page.goto(`file://${join(shots, 'intents-contact-sheet.html')}`);
  await page.setViewportSize({width: 1500, height: 900});
  await page.screenshot({path: join(shots, 'intents-contact-sheet.png'), fullPage: true});
});

test('a pause drain confirmation survives the run reader reconnecting', async ({page}) => {
  const {requests, outcome} = await openIntents(page);
  await page.getByRole('button', {name: 'Park', exact: true}).click();
  await expect(page.getByText('Park · Pending', {exact: true})).toBeVisible();
  await page.evaluate(() => {
    const world = window as unknown as GatewayWindow;
    world.fakeGateway.disconnect();
    world.fakeHost.recent([]);
  });
  await outcome(0, 'applied');
  await expect(page.getByText('Park · Applied', {exact: true})).toBeVisible();
  expect(requests).toHaveLength(1);
  await expect(page.getByText('Parked', {exact: true}).first()).toBeVisible();
});
