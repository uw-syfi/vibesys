import {mkdirSync, writeFileSync} from 'node:fs';
import {join} from 'node:path';
import {create, toBinary} from '@bufbuild/protobuf';
import {expect, test} from '@playwright/test';
import {
  GetBlobsResponseSchema,
  RecordSchema,
  SkeletonResponseSchema,
  TailResponseSchema,
  TailStreamResponseSchema,
} from '@vibesys/proto';
import {fixtureNamed, type StoreRunFixture, storeRunFixture} from '@vibesys/run-fixtures';
import {contactSheetHtml, type SheetEntry} from './checks/contact-sheet.js';
import {screenCheck} from './checks/screen-check.js';
import {settled} from './checks/settle.js';
import {openFixture} from './fixture-world.js';

interface WireFixture {
  skeleton: number[];
  tail: number[];
  blobs: number[];
  log: number[];
}
function wire(fixture: StoreRunFixture): WireFixture {
  const encoded = fixture.records.map(record => toBinary(RecordSchema, record));
  const prefix = fixture.records.slice(0, fixture.initialCount);
  return {
    skeleton: [
      ...toBinary(
        SkeletonResponseSchema,
        create(SkeletonResponseSchema, {
          records: encoded.slice(0, fixture.initialCount),
          committedSeq: fixture.records.at(-1)?.seq ?? 0n,
          nextSeq: (prefix.at(-1)?.seq ?? 0n) + 1n,
          complete: fixture.initialCount === fixture.records.length,
        }),
      ),
    ],
    tail: [
      ...toBinary(
        TailResponseSchema,
        create(TailResponseSchema, {
          records: encoded.slice(fixture.initialCount),
          throughSeq: fixture.records.at(-1)?.seq ?? 0n,
        }),
      ),
    ],
    blobs: [
      ...toBinary(
        GetBlobsResponseSchema,
        create(GetBlobsResponseSchema, {
          blobs: Object.entries(fixture.blobs).map(([sha256, data]) => ({sha256, data})),
        }),
      ),
    ],
    log: [
      ...toBinary(
        TailStreamResponseSchema,
        create(TailStreamResponseSchema, {
          data: fixture.streams['build-log'] ?? new Uint8Array(),
          nextOffset: BigInt(fixture.streams['build-log']?.length ?? 0),
        }),
      ),
    ],
  };
}

function installGateway(input: WireFixture): void {
  const calls: string[] = [];
  const streams = new Set<(bytes: Uint8Array) => void>();
  let active = 0;
  Object.assign(window, {
    vibesysGateway: {
      unary: (_host: string, method: string) => {
        calls.push(method);
        const bytes = method.endsWith('/Skeleton')
          ? input.skeleton
          : method.endsWith('/GetBlobs')
            ? input.blobs
            : undefined;
        if (!bytes) throw new Error('Unknown unary');
        return {result: Promise.resolve(new Uint8Array(bytes)), cancel: () => undefined};
      },
      stream: (
        _host: string,
        method: string,
        _bytes: Uint8Array,
        receive: (bytes: Uint8Array) => void,
      ) => {
        calls.push(method);
        active += 1;
        let closed = false;
        if (method.endsWith('/Tail')) {
          streams.add(receive);
          receive(new Uint8Array(input.tail));
        } else if (method.endsWith('/TailStream')) receive(new Uint8Array(input.log));
        else throw new Error('Unknown stream');
        return {
          cancel: () => {
            if (!closed) {
              closed = true;
              active -= 1;
              streams.delete(receive);
            }
          },
        };
      },
    },
    fakeGateway: {
      calls: () => calls,
      active: () => active,
      append: (bytes: number[]) => {
        for (const receive of streams) receive(new Uint8Array(bytes));
      },
    },
  });
}

const sheet: SheetEntry[] = [];
const shots = process.env['SCREENS_DIR'];
for (const kind of ['short', 'live', 'blobs'] as const)
  for (const width of [1000, 1440, 1920]) {
    test(`store Run screen ${kind} at ${width}px`, async ({page}) => {
      await page.setViewportSize({width, height: 900});
      const fixture = storeRunFixture(kind);
      await page.addInitScript(installGateway, wire(fixture));
      await openFixture(page, {...fixtureNamed('finished-run'), runId: 'run-a'});
      const toggle = page.getByRole('switch', {name: 'Run transport v2'});
      await expect(toggle).not.toBeChecked();
      expect(
        await page.evaluate(() => (window as unknown as GatewayWindow).fakeGateway.calls()),
      ).toEqual([]);
      await toggle.check();
      await expect(page.getByRole('heading', {name: 'queue-rs · mpmc'})).toBeVisible();
      await expect(page.getByRole('button', {name: /Idea 1/}).first()).toBeVisible();
      const range = page.getByRole('slider');
      await expect(range).toHaveCount(1);
      const capture = async (mode: string): Promise<void> => {
        await settled(page);
        expect(await screenCheck(page)).toEqual([]);
        if (!shots) return;
        mkdirSync(shots, {recursive: true});
        const file = `store-${kind}-${width}-${mode}.png`;
        await page.screenshot({path: join(shots, file)});
        sheet.push({name: `${kind}/${width}/${mode}`, file});
      };
      await capture('ideas');
      await range.fill('2');
      await capture('scrubbed');
      const calls = await page.evaluate(() =>
        (window as unknown as GatewayWindow).fakeGateway.calls(),
      );
      expect(calls.filter(method => method.endsWith('/Skeleton'))).toHaveLength(1);
      expect(calls.filter(method => method.endsWith('/GetBlobs'))).toHaveLength(1);
      await page
        .getByRole('button', {name: kind === 'live' ? 'Back to live' : 'Jump to end'})
        .click();
      for (const mode of ['Timeline', 'Agents', 'Usage', 'Logs']) {
        await page.getByRole('radio', {name: mode, exact: true}).click();
        if (mode === 'Logs')
          await expect(page.getByText('Build complete', {exact: true}).last()).toBeVisible();
        await capture(mode.toLowerCase());
      }
      await page.getByRole('radio', {name: 'Ideas', exact: true}).click();
      await page.getByRole('option', {name: /^Idea 1 /}).click();
      await capture('idea');
      await page
        .getByRole('listbox', {name: /steps$/})
        .getByRole('option', {name: /Implementer/i})
        .first()
        .click();
      await expect(page.getByRole('region', {name: 'Implement', exact: true})).toBeVisible();
      await expect(page.getByText('No transcript', {exact: true})).toBeVisible();
      await capture('step');
      await toggle.uncheck();
      await expect(page.getByRole('button', {name: 'Chat', exact: true})).toBeVisible();
      expect(
        await page.evaluate(() => (window as unknown as GatewayWindow).fakeGateway.active()),
      ).toBe(0);
    });
  }

test('store Run app-window contact sheet', async ({page}) => {
  if (!shots || sheet.length === 0) return;
  const html = contactSheetHtml(sheet, 3);
  writeFileSync(join(shots, 'store-contact-sheet.html'), html);
  await page.goto(`file://${join(shots, 'store-contact-sheet.html')}`);
  await page.setViewportSize({width: 1080, height: 900});
  await page.screenshot({path: join(shots, 'store-contact-sheet.png'), fullPage: true});
});
interface GatewayWindow {
  fakeGateway: {calls(): string[]; active(): number; append(bytes: number[]): void};
}
