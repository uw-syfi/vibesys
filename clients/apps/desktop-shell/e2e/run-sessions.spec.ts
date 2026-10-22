import {mkdirSync, writeFileSync} from 'node:fs';
import {join} from 'node:path';
import {create, fromBinary, toBinary} from '@bufbuild/protobuf';
import {expect, type Locator, type Page, test} from '@playwright/test';
import {
  ChatRole,
  type HostCallRequest,
  HostCallRequestSchema,
  HostCallResponseSchema,
  LifecycleSchema,
  Outcome,
  type Record,
  RecordSchema,
  TailResponseSchema,
} from '@vibesys/proto';
import {chatReplay, fixtureNamed, storeRunFixture} from '@vibesys/run-fixtures';
import {contactSheetHtml, type SheetEntry} from './checks/contact-sheet.js';
import {screenCheck} from './checks/screen-check.js';
import {settled} from './checks/settle.js';
import {installGateway, wire} from './fake-gateway.js';
import {openFixture} from './fixture-world.js';

interface GatewayWindow {
  fakeGateway: {appendChat(bytes: number[]): void; active(): number};
}
const shots = process.env['SCREENS_DIR'];
const sheet: SheetEntry[] = [];
for (const width of [1000, 1440, 1920])
  test(`session chat replay and explicit steer at ${width}px`, async ({page}) => {
    await page.setViewportSize({width, height: 900});
    const {fixture, requests} = await prepareSession(page);
    await page.addInitScript(installGateway, fixture);
    await openFixture(page, {...fixtureNamed('queue-rs-live'), runId: 'run-a'});
    await page.getByRole('switch', {name: 'Run transport v2'}).check();
    await page.getByRole('button', {name: 'Chat', exact: true}).click();
    const pane = page.getByRole('region', {name: 'Chat', exact: true}).last();
    await expect(
      pane.getByText('The queue avoids repeated allocation.', {exact: true}),
    ).toBeVisible();
    expect(requests).toHaveLength(0);
    const capture = (state: string) => captureSession(page, width, state);
    await capture('history');
    await pane.getByRole('button', {name: 'Send as steer'}).click();
    await expect(page.getByText('Guidance · Pending', {exact: true})).toBeAttached();
    expect(requests[0]?.method).toMatchObject({
      case: 'postIntent',
      value: {
        intent: {
          case: 'steer',
          value: {text: 'Compare allocation counts before changing the queue.'},
        },
      },
    });
    await pane.getByRole('textbox', {name: 'Message'}).fill('Why did it win?');
    await pane.getByRole('button', {name: 'Send', exact: true}).click();
    await expect.poll(() => requests.length).toBe(2);
    const method = requests[1]?.method;
    if (method?.case !== 'chatMessage') throw new Error('Expected chat-message');
    const requestId = method.value.requestId;
    const question = create(RecordSchema, {
      seq: 6n,
      epoch: 2,
      at: {seconds: 1700000100n},
      body: {
        case: 'chat',
        value: {
          event: {
            case: 'message',
            value: {
              requestId,
              role: ChatRole.USER,
              content: {case: 'text', value: method.value.text},
            },
          },
        },
      },
    });
    const token = create(RecordSchema, {
      seq: 7n,
      epoch: 2,
      at: {seconds: 1700000101n},
      body: {
        case: 'chat',
        value: {event: {case: 'token', value: {requestId, text: 'It reduces contention'}}},
      },
    });
    await page.evaluate(
      bytes => (window as unknown as GatewayWindow).fakeGateway.appendChat(bytes),
      tail([question, token]),
    );
    await expect(pane.getByText('It reduces contention', {exact: true})).toBeVisible();
    await capture('streaming');
    const answer = create(RecordSchema, {
      seq: 8n,
      epoch: 2,
      at: {seconds: 1700000102n},
      body: {
        case: 'chat',
        value: {
          event: {
            case: 'message',
            value: {
              requestId,
              role: ChatRole.ASSISTANT,
              content: {case: 'text', value: 'It reduces contention under load.'},
            },
          },
        },
      },
    });
    await page.evaluate(
      bytes => (window as unknown as GatewayWindow).fakeGateway.appendChat(bytes),
      tail([answer]),
    );
    await expect(pane.getByText('It reduces contention under load.', {exact: true})).toBeVisible();
    await capture('answered');
    await recoverHistory(page, pane, width, answer);
    await pane.getByRole('button', {name: 'Close chat'}).click();
    await expect(page.getByText('Guidance · Pending', {exact: true})).toBeVisible();
    await page.getByRole('switch', {name: 'Run transport v2'}).uncheck();
    expect(
      await page.evaluate(() => (window as unknown as GatewayWindow).fakeGateway.active()),
    ).toBe(0);
  });
for (const state of ['parked', 'stopped'] as const)
  for (const width of [1000, 1440, 1920])
    test(`${state} run retains chat history at ${width}px`, async ({page}) => {
      await page.setViewportSize({width, height: 900});
      const {fixture, requests} = await prepareSession(page, state);
      await page.addInitScript(installGateway, fixture);
      await openFixture(page, {...fixtureNamed('finished-run'), runId: 'run-a'});
      await page.getByRole('switch', {name: 'Run transport v2'}).check();
      await page.getByRole('button', {name: 'Chat', exact: true}).click();
      const pane = page.getByRole('region', {name: 'Chat', exact: true}).last();
      await expect(
        pane.getByText('The queue avoids repeated allocation.', {exact: true}),
      ).toBeVisible();
      await captureSession(page, width, `${state}-history`);
      expect(requests).toHaveLength(0);
      await pane.getByRole('button', {name: 'Close chat'}).click();
      await expect(page.getByRole('button', {name: 'Resume', exact: true})).toBeVisible();
    });

test('session app-window contact sheet', async ({page}) => {
  if (!shots || sheet.length === 0) return;
  const html = contactSheetHtml(sheet, 3);
  writeFileSync(join(shots, 'session-contact-sheet.html'), html);
  await page.goto(`file://${join(shots, 'session-contact-sheet.html')}`);
  await page.setViewportSize({width: 1080, height: 900});
  await page.screenshot({path: join(shots, 'session-contact-sheet.png'), fullPage: true});
});

const tail = (records: ReturnType<typeof chatReplay>) => [
  ...toBinary(
    TailResponseSchema,
    create(TailResponseSchema, {
      records: records.map(record => toBinary(RecordSchema, record)),
    }),
  ),
];

async function prepareSession(page: Page, state: 'live' | 'parked' | 'stopped' = 'live') {
  const source = storeRunFixture(state === 'live' ? 'live' : 'short');
  if (state !== 'live') {
    const last = source.records.at(-1);
    if (last?.body.case !== 'lifecycle') throw new Error('Missing lifecycle');
    last.body.value = create(LifecycleSchema, {
      state:
        state === 'parked'
          ? {case: 'parked', value: {}}
          : {case: 'ended', value: {outcome: Outcome.STOPPED}},
    });
  }
  const fixture = wire(source);
  fixture.chat = tail(chatReplay());
  const requests: HostCallRequest[] = [];
  await page.exposeFunction('postIntent', (_host: string, bytes: number[]) => {
    const envelope = fromBinary(HostCallRequestSchema, new Uint8Array(bytes));
    requests.push(envelope);
    const result =
      envelope.method.case === 'chatMessage'
        ? {
            case: 'chatMessage' as const,
            value: {
              requestId: envelope.method.value.requestId,
              sessionId: envelope.method.value.sessionId,
            },
          }
        : {
            case: 'postIntent' as const,
            value: {
              requestId:
                envelope.method.case === 'postIntent' ? envelope.method.value.requestId : '',
              orderedReader: true,
            },
          };
    return [...toBinary(HostCallResponseSchema, create(HostCallResponseSchema, {result}))];
  });
  return {fixture, requests};
}

async function captureSession(page: Page, width: number, state: string): Promise<void> {
  await settled(page);
  expect(await screenCheck(page)).toEqual([]);
  if (!shots) return;
  mkdirSync(shots, {recursive: true});
  const file = `session-${width}-${state}.png`;
  await page.screenshot({path: join(shots, file)});
  sheet.push({name: `${width}/${state}`, file});
}

async function recoverHistory(page: Page, pane: Locator, width: number, answer: Record) {
  await page.evaluate(
    bytes => (window as unknown as GatewayWindow).fakeGateway.appendChat(bytes),
    tail([create(RecordSchema, {...answer, seq: 10n})]),
  );
  await expect(pane.getByText('Unavailable', {exact: true})).toBeVisible();
  await captureSession(page, width, 'unavailable');
  await pane.getByRole('button', {name: 'Retry history'}).click();
  await expect(pane.getByText('Unavailable', {exact: true})).not.toBeVisible();
}
