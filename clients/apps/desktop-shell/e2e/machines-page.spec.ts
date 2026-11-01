import AxeBuilder from '@axe-core/playwright';
import {expect, test} from '@playwright/test';
import {card, type MachinesWorld, openMachines, testOf, WORLDS} from './machines-page.js';

function world(name: string): MachinesWorld {
  const found = WORLDS.find(each => each.name === name);
  if (found === undefined) throw new Error(`no world named ${name}`);
  return found;
}

test('the Test column has no header word, and each Test button is centred on its row', async ({
  page,
}) => {
  await openMachines(page, 1440, world('several machines'));
  const mac = card(page, 'This Mac');
  const header = mac.getByRole('columnheader', {name: 'Test'}).locator('span');
  expect((await header.boundingBox())?.width).toBeLessThanOrEqual(1);
  const name = mac
    .getByRole('row', {name: /lab-h100/})
    .getByRole('cell')
    .first();
  const button = testOf(page, 'This Mac', 'lab-h100');
  const buttonBox = await button.boundingBox();
  const middle = (box: {y: number; height: number} | null) =>
    (box?.y ?? 0) + (box?.height ?? 0) / 2;
  // The button's middle is the row text's middle, to within a pixel.
  const text = await name.evaluate(el => {
    const range = document.createRange();
    range.selectNodeContents(el);
    const rect = range.getBoundingClientRect();
    return rect.y + rect.height / 2;
  });
  expect(Math.abs(middle(buttonBox) - text)).toBeLessThanOrEqual(1);
});

test('the Machines page has no accessibility violations', async ({page}) => {
  await openMachines(page, 1440, world('several machines'));
  const axe = await new AxeBuilder({page}).analyze();
  expect(axe.violations).toEqual([]);
});
