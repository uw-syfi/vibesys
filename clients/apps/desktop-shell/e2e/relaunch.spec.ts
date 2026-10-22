/** Relaunch through the shipping Run screen's header and its existing skeleton fold. */
import {mkdirSync, writeFileSync} from 'node:fs';
import {join} from 'node:path';
import {clone, create, fromBinary, toBinary} from '@bufbuild/protobuf';
import {TimestampSchema} from '@bufbuild/protobuf/wkt';
import {expect, type Page, test} from '@playwright/test';
import {
  HostCallRequestSchema,
  HostCallResponseSchema,
  LifecycleSchema,
  Outcome,
  RecordSchema,
  RelaunchRefusal,
  RunLiveness,
  TailResponseSchema,
  WatchRunsResponseSchema,
} from '@vibesys/proto';
import {fixtureNamed, type StoreRunFixture, storeRunFixture} from '@vibesys/run-fixtures';
import {contactSheetHtml, type SheetEntry} from './checks/contact-sheet.js';
import {screenCheck} from './checks/screen-check.js';
import {settled} from './checks/settle.js';
import {installGateway, wire} from './fake-gateway.js';
import {openFixture} from './fixture-world.js';

type RunState = 'parked' | 'stopped-resumable' | 'interrupted' | 'finished' | 'failed';
interface GatewayWindow {
  fakeGateway: {append(bytes: number[]): void; observe(bytes: number[]): void};
}
function replay(state: RunState): StoreRunFixture {
  const fixture = storeRunFixture('short', 2, 3);
  const records = fixture.records.map(record => clone(RecordSchema, record));
  const last = records.at(-1);
  if (last?.body.case !== 'lifecycle') throw new Error('Missing lifecycle');
  if (state === 'interrupted') records.pop();
  else
    last.body.value = create(LifecycleSchema, {
      state:
        state === 'parked'
          ? {case: 'parked', value: {}}
          : {
              case: 'ended',
              value: {
                outcome:
                  state === 'finished'
                    ? Outcome.FINISHED
                    : state === 'failed'
                      ? Outcome.FAILED
                      : Outcome.STOPPED,
              },
            },
    });
  return {...fixture, records, initialCount: records.length};
}
async function openRelaunch(page: Page, state: RunState, result = 'epoch') {
  const fixture = replay(state);
  let answer: ((bytes: number[]) => void) | undefined;
  await page.exposeFunction('postIntent', (_host: string, bytes: number[]) => {
    const request = fromBinary(HostCallRequestSchema, new Uint8Array(bytes));
    expect(request.method).toMatchObject({
      case: 'relaunch',
      value: {
        runId: 'run-a',
        projectRoot: '/work/queue-rs',
        expectedEpoch: 1n,
      },
    });
    if (result === 'uncertain') throw new Error('Connection ended before acknowledgement');
    return new Promise<number[]>(resolve => {
      answer = resolve;
    });
  });
  await page.addInitScript(
    installGateway,
    wire(fixture, state === 'interrupted' ? RunLiveness.INTERRUPTED : RunLiveness.ENDED),
  );
  await openFixture(page, {...fixtureNamed('finished-run'), runId: 'run-a'});
  await page.getByRole('switch', {name: 'Run transport v2'}).check();
  await expect(page.getByRole('button', {name: 'Resume', exact: true})).toBeEnabled();
  return {
    fixture,
    release: () => {
      const refusal = {
        locked: RelaunchRefusal.LOCKED,
        checkout: RelaunchRefusal.CHECKOUT_MISSING,
        changed: RelaunchRefusal.EPOCH_CHANGED,
      }[result];
      answer?.([
        ...toBinary(
          HostCallResponseSchema,
          create(HostCallResponseSchema, {
            result: {
              case: 'relaunch',
              value: {
                outcome:
                  refusal === undefined
                    ? {case: 'epoch', value: 2n}
                    : {case: 'refusal', value: refusal},
              },
            },
          }),
        ),
      ]);
    },
  };
}
async function newEpoch(page: Page, fixture: StoreRunFixture): Promise<void> {
  const first = fixture.records.find(record => record.body.case === 'epochStarted');
  const last = fixture.records.at(-1);
  if (!first || !last) throw new Error('Missing epoch');
  const epoch = clone(RecordSchema, first);
  epoch.seq = last.seq + 1n;
  epoch.epoch = 2;
  epoch.at = create(TimestampSchema, {seconds: (last.at?.seconds ?? 0n) + 3600n});
  if (last.clock !== undefined) epoch.clock = last.clock;
  if (epoch.body.case !== 'epochStarted') throw new Error('Missing header');
  epoch.body.value.epoch = 2;
  const running = create(RecordSchema, {
    seq: epoch.seq + 1n,
    epoch: 2,
    at: epoch.at,
    body: {case: 'lifecycle', value: {state: {case: 'running', value: {}}}},
  });
  const tail = [
    ...toBinary(
      TailResponseSchema,
      create(TailResponseSchema, {
        records: [epoch, running].map(record => toBinary(RecordSchema, record)),
        throughSeq: running.seq,
      }),
    ),
  ];
  const watch = [
    ...toBinary(
      WatchRunsResponseSchema,
      create(WatchRunsResponseSchema, {
        runId: 'run-a',
        liveness: RunLiveness.ALIVE,
        head: {
          strategy: 'fixture',
          lifecycle: running.body.case === 'lifecycle' ? running.body.value : undefined,
          process: {pid: 1, epoch: 2, startedAt: epoch.at},
        },
      }),
    ),
  ];
  await page.evaluate(
    ({tail, watch}) => {
      const gateway = (window as unknown as GatewayWindow).fakeGateway;
      gateway.observe(watch);
      gateway.append(tail);
    },
    {tail, watch},
  );
}
test('Relaunch header action, results and new epoch survive Tail before acknowledgement', async ({
  context,
}) => {
  test.setTimeout(90_000);
  const shots = process.env['SCREENS_DIR'];
  const entries: SheetEntry[] = [];
  const capture = async (page: Page, name: string): Promise<void> => {
    await settled(page);
    expect(await screenCheck(page)).toEqual([]);
    if (!shots) return;
    mkdirSync(shots, {recursive: true});
    const file = `${name}.png`;
    await page.screenshot({path: join(shots, file)});
    entries.push({name, file});
  };
  for (const width of [1000, 1440, 1920]) {
    for (const state of [
      'parked',
      'stopped-resumable',
      'interrupted',
      'finished',
      'failed',
    ] as const) {
      const page = await context.newPage();
      await page.setViewportSize({width, height: 900});
      const world = await openRelaunch(page, state);
      await capture(page, `${state}-${width}-ready`);
      await page.getByRole('button', {name: 'Resume', exact: true}).click();
      await expect(page.getByRole('button', {name: 'Resume', exact: true})).toBeDisabled();
      await capture(page, `${state}-${width}-launching`);
      await newEpoch(page, world.fixture);
      await expect(page.getByRole('button', {name: 'Resume', exact: true})).toBeVisible();
      world.release();
      await expect(page.getByText('Epoch 2', {exact: true})).toBeVisible();
      await capture(page, `${state}-${width}-epoch`);
      await page.close();
    }
  }
  for (const [result, label] of [
    ['locked', 'Refused: locked'],
    ['checkout', 'Refused: checkout missing'],
    ['changed', 'Refused: epoch changed'],
    ['uncertain', 'Resume not confirmed. Check the timeline before trying again.'],
  ] as const) {
    const page = await context.newPage();
    await page.setViewportSize({width: 1000, height: 900});
    const world = await openRelaunch(page, 'interrupted', result);
    await page.getByRole('button', {name: 'Resume', exact: true}).click();
    world.release();
    await expect(page.getByText(label, {exact: true})).toBeVisible();
    await capture(page, result);
    await page.close();
  }
  if (!shots) return;
  const page = await context.newPage();
  writeFileSync(join(shots, 'relaunch-contact-sheet.html'), contactSheetHtml(entries, 3));
  await page.goto(`file://${join(shots, 'relaunch-contact-sheet.html')}`);
  await page.screenshot({path: join(shots, 'relaunch-contact-sheet.png'), fullPage: true});
});
