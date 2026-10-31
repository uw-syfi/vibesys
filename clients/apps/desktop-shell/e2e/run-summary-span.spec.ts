import {expect, test} from '@playwright/test';
import {fixtureNamed} from '@vibesys/run-fixtures';
import {settled} from './checks/settle.js';
import {anchorsOf, RUN_ANCHORS, stabilityViolations} from './checks/stability.js';
import {openFixture} from './fixture-world.js';

/**
 * A finished run opened over the gateway: its ideas and steps come from the run log's skeleton, a
 * step reads only its own content, the time bar is drawn whole at the end, and opening a step moves
 * nothing. The run is a committed fixture the shipping projection turns into native records.
 */
const fixture = fixtureNamed('finished-run');
const gatewayCalls = (page: import('@playwright/test').Page) =>
  page.evaluate(() =>
    (window as unknown as {fakeGateway: {calls(): string[]}}).fakeGateway.calls(),
  );

test('a finished run opens from its skeleton, and a step reads only its own content', async ({
  page,
}) => {
  await openFixture(page, fixture);
  await expect(page.getByRole('listbox', {name: 'Ideas'})).toBeVisible();
  const opened = await gatewayCalls(page);
  expect(opened.filter(method => method.endsWith('/Skeleton')).length).toBeGreaterThan(0);

  // An idea that is not the run's adopted one: the list opens sorted by result, the adopted first.
  await page
    .getByRole('listbox', {name: 'Ideas'})
    .getByRole('option', {name: /^Per-core queues with stealing/})
    .click();
  const steps = page.getByRole('listbox', {name: /steps$/});
  await expect(steps).toBeVisible();
  const before = await gatewayCalls(page);
  await steps
    .getByRole('option', {name: /builder/i})
    .first()
    .click();
  await expect(steps).toBeVisible();
  await settled(page);
  // Opening a step asks the host for at most its own content, never the log or the skeleton again.
  const asked = (await gatewayCalls(page)).slice(before.length);
  expect(asked.filter(method => !method.endsWith('/GetBlobs'))).toEqual([]);
});

test('regression: a finished run has its time bar whole, at the end', async ({page}) => {
  await openFixture(page, fixture);
  await expect(page.getByRole('listbox', {name: 'Ideas'})).toBeVisible();
  const bar = page.getByRole('slider', {name: 'Run position'});
  await expect(bar).toBeVisible();
  // The ended run's trace pages in, so the track is drawn whole from its first position to its
  // last, with the playhead at the end.
  const {min, max, value, disabled} = await bar.evaluate(range => {
    const input = range as HTMLInputElement;
    return {min: input.min, max: input.max, value: input.value, disabled: input.disabled};
  });
  expect({min, atEnd: value === max, movable: !disabled && Number(max) > 0}).toEqual({
    min: '0',
    atEnd: true,
    movable: true,
  });
  await expect(page.getByText('End', {exact: true}).first()).toBeVisible();
});

test('regression: opening a step moves neither pane, the chart nor the time bar', async ({
  page,
}) => {
  await openFixture(page, fixture);
  await page.getByRole('listbox', {name: 'Ideas'}).getByRole('option').first().click();
  const steps = page.getByRole('listbox', {name: /steps$/});
  await expect(steps).toBeVisible();
  await settled(page);
  const before = await anchorsOf(page, RUN_ANCHORS);
  const panesTop = () =>
    page.locator('.vs-panes').evaluate(panes => panes.getBoundingClientRect().y);
  const top = await panesTop();
  const columns = () =>
    page.locator('.vs-panes > .vs-pane').evaluateAll(panes =>
      panes.map(pane => {
        const {x, width} = pane.getBoundingClientRect();
        return {x, width};
      }),
    );
  const placed = await columns();
  await steps
    .getByRole('option', {name: /builder/i})
    .first()
    .click();
  await expect(page.getByRole('navigation', {name: 'Zoom'})).toBeVisible();
  await settled(page);
  expect(await panesTop()).toBe(top);
  // Horizontally too: the list beside the detail keeps its width, so the detail keeps its edge.
  expect(await columns()).toEqual(placed);
  expect(await stabilityViolations(page, 'step', before, RUN_ANCHORS)).toEqual([]);
});
