import {mkdirSync} from 'node:fs';
import {join} from 'node:path';
import {create, toBinary} from '@bufbuild/protobuf';
import {expect, test} from '@playwright/test';
import {RecordSchema, TailResponseSchema} from '@vibesys/proto';
import {fixtureNamed} from '@vibesys/run-fixtures';
import {screenCheck} from './checks/screen-check.js';
import {settled} from './checks/settle.js';
import {openFixture} from './fixture-world.js';

function batch(seq: bigint): number[] {
  return [
    ...toBinary(
      TailResponseSchema,
      create(TailResponseSchema, {
        throughSeq: seq,
        records: [
          toBinary(
            RecordSchema,
            create(RecordSchema, {
              seq,
              epoch: 3,
              body: {case: 'legacyEvent', value: {json: '{"type":"run_started"}'}},
            }),
          ),
        ],
      }),
    ),
  ];
}

for (const width of [1000, 1440]) {
  test(`raw records opt in, append and cancel at ${width}px`, async ({page}) => {
    await page.setViewportSize({width, height: 900});
    await page.addInitScript(initial => {
      const listeners = new Set<(bytes: Uint8Array) => void>();
      let starts = 0;
      Object.assign(window, {
        vibesysGateway: {
          unary: () => {
            throw new Error('unary is not offered by this Tail fixture');
          },
          stream: (
            _host: string,
            method: string,
            _bytes: Uint8Array,
            receive: (bytes: Uint8Array) => void,
          ) => {
            if (method !== '/vibesys.api.v1.HostService/Tail')
              throw new Error('unknown fixture method');
            starts += 1;
            listeners.add(receive);
            receive(new Uint8Array(initial));
            return {cancel: () => listeners.delete(receive)};
          },
        },
        fakeGateway: {
          starts: () => starts,
          active: () => listeners.size,
          append: (bytes: number[]) => {
            for (const receive of listeners) receive(new Uint8Array(bytes));
          },
        },
      });
    }, batch(1n));
    await openFixture(page, fixtureNamed('crashed-after-turn'));
    const toggle = page.getByRole('switch', {name: 'Run transport v2'});
    await expect(toggle).not.toBeChecked();
    await expect(toggle).toBeEnabled();
    expect(
      await page.evaluate(() => (window as unknown as GatewayWindow).fakeGateway.starts()),
    ).toBe(0);
    await toggle.check();
    const table = page.getByRole('table', {name: 'Raw records'});
    await expect(table.getByRole('row')).toHaveCount(2);
    await expect(table).toContainText('legacyEvent');
    await expect(table).toContainText('run_started');
    await page.evaluate(
      bytes => (window as unknown as GatewayWindow).fakeGateway.append(bytes),
      batch(2n),
    );
    await expect(table.getByRole('row')).toHaveCount(3);
    await table.scrollIntoViewIfNeeded();
    await settled(page);
    expect(await screenCheck(page)).toEqual([]);
    const shots = process.env['SCREENS_DIR'];
    if (shots !== undefined) {
      mkdirSync(shots, {recursive: true});
      await page.screenshot({path: join(shots, `gateway-records-${width}.png`)});
    }
    await toggle.uncheck();
    await expect(table).toHaveCount(0);
    expect(
      await page.evaluate(() => (window as unknown as GatewayWindow).fakeGateway.active()),
    ).toBe(0);
  });
}
interface GatewayWindow {
  fakeGateway: {starts(): number; active(): number; append(bytes: number[]): void};
}
