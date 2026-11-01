import {mkdirSync, writeFileSync} from 'node:fs';
import {join} from 'node:path';
import {clone, create, fromBinary, toBinary} from '@bufbuild/protobuf';
import {expect, type Page, test} from '@playwright/test';
import {
  Change,
  HostCallRequestSchema,
  HostCallResponseSchema,
  IntentAction,
  IntentStep,
  LifecycleSchema,
  Outcome,
  type PostIntentRequest,
  RecordSchema,
  RelaunchRefusal,
  RunEntrySchema,
  RunLiveness,
  SpanStatus,
  type Record as StoreRecord,
  TailResponseSchema,
} from '@vibesys/proto';
import {fixtureNamed, INTENT_REPLAYS, intentReplay, storeRunFixture} from '@vibesys/run-fixtures';
import {contactSheetHtml, type SheetEntry} from './checks/contact-sheet.js';
import {screenCheck} from './checks/screen-check.js';
import {type Rest, settled} from './checks/settle.js';
import {installGateway, wire} from './fake-gateway.js';
import {openFixture} from './fixture-world.js';

interface GatewayWindow {
  fakeGateway: {
    calls(): string[];
    active(): number;
    append(bytes: number[]): void;
    disconnect(): void;
    observe(bytes: number[]): void;
  };
  fakeHost: {calls: unknown[][]; recent(runs: readonly unknown[]): void};
}

function appliedState(action: IntentAction) {
  return create(LifecycleSchema, {
    state:
      action === IntentAction.PAUSE_NOW
        ? {case: 'parked', value: {}}
        : {case: 'draining', value: {}},
  }).state;
}

async function openIntents(page: Page, options: {amendment?: boolean; history?: 'pending'} = {}) {
  const source = storeRunFixture('live');
  const fixture = {...source, records: [...source.records]};
  if (options.amendment) {
    const header = fixture.records.find(record => record.body.case === 'epochStarted');
    if (header?.body.case !== 'epochStarted') throw new Error('fixture header required');
    fixture.records.push(
      create(RecordSchema, {
        seq: (fixture.records.at(-1)?.seq ?? 0n) + 1n,
        epoch: header.epoch,
        at: {seconds: 1700000200n},
        body: {case: 'declarationAmended', value: header.body.value},
      }),
    );
  }
  const requests: {host: string; request: PostIntentRequest}[] = [];
  let sequence = fixture.records.at(-1)?.seq ?? 0n;
  await page.exposeFunction('postIntent', (host: string, bytes: number[]) => {
    const envelope = fromBinary(HostCallRequestSchema, new Uint8Array(bytes));
    if (envelope.method.case === 'relaunch')
      return [
        ...toBinary(
          HostCallResponseSchema,
          create(HostCallResponseSchema, {
            result: {case: 'relaunch', value: {outcome: {case: 'epoch', value: 2n}}},
          }),
        ),
      ];
    if (envelope.method.case !== 'postIntent') throw new Error('Expected PostIntent');
    requests.push({host, request: envelope.method.value});
    const replay = intentReplay('pending', envelope.method.value.requestId);
    return [...toBinary(HostCallResponseSchema, replay.admission)];
  });
  await installGateway(page, intentWire(fixture, options.history));
  await openFixture(page, {...fixtureNamed('queue-rs-live'), runId: 'run-a'});
  if (options.history !== 'pending')
    await expect(page.getByRole('button', {name: 'Guidance', exact: true})).toBeVisible();
  const outcome = async (index: number, result: 'applied' | 'refused'): Promise<void> => {
    const request = requests[index]?.request;
    if (!request) throw new Error('No posted intent');
    const action = request.intent.case === 'pauseNow' ? IntentAction.PAUSE_NOW : IntentAction.PAUSE;
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

  return {
    requests,
    outcome,
    append: (records: StoreRecord[]) => appendRecords(page, records),
    observe: (epoch: number, liveness: RunLiveness) => observeWriter(page, epoch, liveness),
    next: () => ++sequence,
    fixture,
  };
}

function intentWire(fixture: ReturnType<typeof storeRunFixture>, history: 'pending' | undefined) {
  const input = wire(fixture);
  if (history === 'pending') {
    input.tail = [
      ...toBinary(
        TailResponseSchema,
        create(TailResponseSchema, {
          throughSeq: fixture.records[fixture.initialCount - 1]?.seq ?? 0n,
        }),
      ),
    ];
  }
  return input;
}

async function appendRecords(page: Page, records: StoreRecord[]) {
  const sequence = records.at(-1)?.seq;
  if (sequence === undefined) throw new Error('records required');
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
}
function finishOpenSpans(records: StoreRecord[], next: () => bigint): StoreRecord[] {
  const spans = new Map(
    records.flatMap(record =>
      record.body.case === 'span' && record.body.value.span
        ? [[record.body.value.span.id, record.body.value.span] as const]
        : [],
    ),
  );
  return [...spans.values()]
    .filter(span => span.endedAt === undefined)
    .map(span =>
      create(RecordSchema, {
        seq: next(),
        epoch: 2,
        body: {
          case: 'span',
          value: {
            change: Change.ENDED,
            span: {...span, status: SpanStatus.STOPPED, endedAt: Math.max(10, span.startedAt)},
          },
        },
      }),
    );
}

async function observeWriter(page: Page, epoch: number, liveness: RunLiveness) {
  const bytes = [...toBinary(RunEntrySchema, create(RunEntrySchema, {liveness, head: {epoch}}))];
  await page.evaluate(
    bytes => (window as unknown as GatewayWindow).fakeGateway.observe(bytes),
    bytes,
  );
}

for (const width of [800, 1440]) {
  test(`declaration amendments keep lifecycle and addressed Pause at ${width}px`, async ({
    page,
  }) => {
    await page.setViewportSize({width, height: 900});
    const {requests} = await openIntents(page, {amendment: true});
    await expect(page.getByText('Saved run', {exact: true})).toHaveCount(0);
    await expect(page.getByRole('button', {name: 'Resume', exact: true})).toHaveCount(0);
    await page.getByRole('button', {name: 'Pause', exact: true}).click();
    await expect(page.getByText('Pause · Pending', {exact: true})).toBeVisible();
    expect(requests).toHaveLength(1);
    expect(requests[0]?.request.projectRoot).toBe('/work/queue-rs');
    expect(requests[0]?.request.expectedEpoch).toBe(1n);
    await capture(page, `amendment-pause-${width}`);
  });
}

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

test('Pause escalates to Pause now in the same epoch and waits for journal outcomes', async ({
  page,
}) => {
  const {requests, outcome} = await openIntents(page);
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
  await expect(page.getByText('Pausing', {exact: true}).first()).toBeVisible();
  // An applied request has no row, and the pending one is gone.
  await expect(page.getByText(/^Pause · /)).toHaveCount(0);
  await page.getByRole('button', {name: 'Pause now', exact: true}).click();
  await expect(page.getByText('Pause now · Pending', {exact: true})).toBeVisible();
  expect(requests[1]?.request.intent.case).toBe('pauseNow');
  expect(requests[1]?.request.requestId).not.toBe(requests[0]?.request.requestId);
  await outcome(1, 'applied');
  await expect(page.getByText('Paused', {exact: true}).first()).toBeVisible();
  await expect(page.getByText(/^Pause now · /)).toHaveCount(0);
  expect(
    await page.evaluate(() =>
      (window as unknown as GatewayWindow).fakeHost.calls.filter(call => call[0] === 'resume'),
    ),
  ).toEqual([]);
  await page.getByRole('button', {name: 'Settings', exact: true}).click();
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
      const action = 'Pause';
      await page.getByRole('button', {name: action, exact: true}).click();
      if (kind !== 'pending') {
        await expect(page.getByText(`${action} · Pending`, {exact: true})).toBeVisible();
        await outcome(0, kind === 'park-applied' ? 'applied' : kind);
      }
      // A request still on its way, or refused, keeps its row; an applied one has none.
      const status = kind === 'pending' ? 'Pending' : kind === 'refused' ? 'Refused' : null;
      if (status === null) await expect(page.getByText(`${action} · `)).toHaveCount(0);
      else await expect(page.getByText(`${action} · ${status}`, {exact: true})).toBeVisible();
      expect(requests).toHaveLength(1);
      if (kind === 'park-applied') {
        expect(requests[0]?.request.intent.case).toBe('pause');
        await expect(page.getByText('Pausing', {exact: true}).first()).toBeVisible();
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

test('live controls remain reachable and history follows pause, resume and finish', async ({
  page,
}) => {
  await page.setViewportSize({width: 800, height: 900});
  const {outcome, append, observe, next, fixture} = await openIntents(page);
  const pauseNow = page.getByRole('button', {name: 'Pause now', exact: true});
  await expect(pauseNow).toBeEnabled();
  await expect(pauseNow).toBeInViewport();
  await capture(page, 'controls-live-v2');
  await page.getByRole('button', {name: 'Pause', exact: true}).click();
  await expect(page.getByText('Pause · Pending', {exact: true})).toBeVisible();
  await outcome(0, 'applied');
  await expect(page.getByText('Pausing', {exact: true}).first()).toBeVisible();
  await expect(pauseNow).toBeEnabled();
  await capture(page, 'controls-draining');
  await pauseNow.click();
  await expect(page.getByText('Pause now · Pending', {exact: true})).toBeVisible();
  await outcome(1, 'applied');
  await observe(1, RunLiveness.ENDED);
  const resume = page.getByRole('button', {name: 'Resume', exact: true});
  await expect(resume).toBeEnabled();
  await expect(page.getByText('Paused', {exact: true}).first()).toBeVisible();
  await expect(pauseNow).toHaveCount(0);
  await capture(page, 'controls-parked');
  await resume.click();
  const header = fixture.records.find(record => record.body.case === 'epochStarted');
  if (header?.body.case !== 'epochStarted') throw new Error('epoch header required');
  await append([
    create(RecordSchema, {
      seq: next(),
      epoch: 2,
      body: {case: 'epochStarted', value: {...header.body.value, epoch: 2}},
    }),
    create(RecordSchema, {
      seq: next(),
      epoch: 2,
      body: {case: 'lifecycle', value: {state: {case: 'running', value: {}}}},
    }),
    create(RecordSchema, {
      seq: next(),
      epoch: 2,
      body: {
        case: 'intent',
        value: {controlId: 'resume-2', action: IntentAction.RESUME, step: IntentStep.APPLIED},
      },
    }),
  ]);
  await observe(2, RunLiveness.ALIVE);
  await expect(pauseNow).toBeEnabled();
  await expect(page.getByText(/ · Applied/)).toHaveCount(0);
  await capture(page, 'controls-resumed');
  await append([
    ...finishOpenSpans(fixture.records, next),
    create(RecordSchema, {
      seq: next(),
      epoch: 2,
      body: {
        case: 'lifecycle',
        value: {state: {case: 'ended', value: {outcome: Outcome.FINISHED, resumable: false}}},
      },
    }),
  ]);
  await observe(2, RunLiveness.ENDED);
  await expect(page.getByRole('button', {name: 'Pause', exact: true})).toHaveCount(0);
  await expect(pauseNow).toHaveCount(0);
  // The host admits Resume after any ending, whatever flag the ended record keeps.
  await expect(resume).toBeVisible();
  await expect(page.getByText('Running', {exact: true})).toHaveCount(0);
  await capture(page, 'controls-finished');
});

test('Pause now is reachable while a live run is still loading history', async ({page}) => {
  await page.setViewportSize({width: 800, height: 900});
  const {requests, append, fixture} = await openIntents(page, {history: 'pending'});
  await expect(page.getByText(/^History \d+ of \d+$/)).toBeVisible();
  const pauseNow = page.getByRole('button', {name: 'Pause now', exact: true});
  await expect(pauseNow).toBeEnabled();
  await expect(pauseNow).toBeInViewport();
  await capture(page, 'controls-loading-v2', 'as shown');
  await pauseNow.click();
  await expect(page.getByText('Pause now · Pending', {exact: true})).toBeVisible();
  expect(requests[0]?.request.intent.case).toBe('pauseNow');
  await append(fixture.records.slice(fixture.initialCount));
  await expect(page.getByText(/^History \d+ of \d+$/)).toHaveCount(0);
  await expect(pauseNow).toBeEnabled();
  await expect(page.getByText('Pause now · Pending', {exact: true})).toBeVisible();
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
  await page.getByRole('button', {name: 'Pause', exact: true}).click();
  await expect(page.getByText('Pause · Pending', {exact: true})).toBeVisible();
  await page.evaluate(() => {
    const world = window as unknown as GatewayWindow;
    world.fakeGateway.disconnect();
    world.fakeHost.recent([]);
  });
  await outcome(0, 'applied');
  expect(requests).toHaveLength(1);
  await expect(page.getByText('Pausing', {exact: true}).first()).toBeVisible();
  await expect(page.getByText(/^Pause · /)).toHaveCount(0);
});

test('a catalog-addressed historical run offers Resume without optional epoch context', async ({
  page,
}) => {
  const source = storeRunFixture('short');
  const fixture = {
    ...source,
    records: source.records.map(record => {
      const copy = clone(RecordSchema, record);
      if (copy.body.case === 'epochStarted') copy.body.value.projectRoot = undefined;
      return copy;
    }),
  };
  const requests: {projectRoot: string; runId: string}[] = [];
  await page.exposeFunction('postIntent', (_host: string, bytes: number[]) => {
    const envelope = fromBinary(HostCallRequestSchema, new Uint8Array(bytes));
    if (envelope.method.case !== 'relaunch') throw new Error('Expected Resume');
    requests.push(envelope.method.value);
    return [
      ...toBinary(
        HostCallResponseSchema,
        create(HostCallResponseSchema, {
          result: {
            case: 'relaunch',
            value: {outcome: {case: 'refusal', value: RelaunchRefusal.CHECKOUT_MISSING}},
          },
        }),
      ),
    ];
  });
  await installGateway(page, wire(fixture));
  await openFixture(page, {...fixtureNamed('finished-run'), runId: 'run-a'});
  await page.getByRole('button', {name: 'Resume', exact: true}).click();
  await expect(page.getByText('Resume · Refused: checkout missing', {exact: true})).toBeVisible();
  expect(requests).toHaveLength(1);
  expect(requests[0]).toMatchObject({projectRoot: '/work/queue-rs', runId: 'run-a'});
  await settled(page);
  expect(await screenCheck(page)).toEqual([]);
});
