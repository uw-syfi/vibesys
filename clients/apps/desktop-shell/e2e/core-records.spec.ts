import {expect, type Page, test} from '@playwright/test';
import {fixtureNamed, storeRunFixture, withCoreRecords} from '@vibesys/run-fixtures';
import {screenCheck} from './checks/screen-check.js';
import {settled} from './checks/settle.js';
import {installGateway, wire} from './fake-gateway.js';
import {openFixture} from './fixture-world.js';

const MODES = ['Ideas', 'Timeline', 'Agents', 'Usage', 'Logs'];

/** Every face of the Run screen as text, after the screen checks pass on it. */
async function faces(page: Page, records: ReturnType<typeof storeRunFixture>): Promise<string[]> {
  await page.setViewportSize({width: 1440, height: 900});
  await installGateway(page, wire(records));
  await openFixture(page, {...fixtureNamed('finished-run'), runId: 'run-a'});
  const texts: string[] = [];
  for (const mode of MODES) {
    await page.getByRole('radio', {name: mode, exact: true}).click();
    await settled(page);
    expect(await screenCheck(page)).toEqual([]);
    texts.push(await page.locator('body').innerText());
  }
  return texts;
}

for (const kind of ['short', 'live'] as const)
  test(`core records among the ordinary ones change no screen of the ${kind} run`, async ({
    page,
  }) => {
    const plain = storeRunFixture(kind);
    const records = withCoreRecords(plain.records, [2, 1, 0, 3, 1, 2, 4, 1, 2]);
    const mixed = {
      ...plain,
      records,
      initialCount: plain.initialCount + records.length - plain.records.length,
    };
    expect(records.length).toBeGreaterThan(plain.records.length);
    const before = await faces(page, plain);
    const after = await faces(await page.context().newPage(), mixed);
    expect(after).toEqual(before);
    for (const text of after) expect(text).not.toMatch(/Core input|attempt \d+ started|Run ended/);
  });
