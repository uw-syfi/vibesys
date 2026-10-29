import {expect, test} from '@playwright/test';
import {checkControls, controlsOf} from './checks/controls-act.js';

for (const resets of [false, true]) {
  test(`the control audit reads a grouped select's displayed text${resets ? ' after it resets' : ''}`, async ({
    browser,
    page,
  }) => {
    const html = `<main><select aria-label="Model" ${resets ? 'onchange="this.value=\'run\'"' : ''}>
      <option value="run">Same as the run</option>
      <optgroup label="Models"><option value="other">Other model</option></optgroup>
    </select></main>`;
    await page.setContent(html);
    const controls = await controlsOf(page.getByRole('main'));
    expect(controls).toHaveLength(1);
    const found = await checkControls(
      browser,
      {},
      controls,
      async copy => {
        await copy.setContent(html);
      },
      copy => copy.getByRole('main'),
    );
    expect(found).toEqual([resets ? 'combobox "Model" changes nothing on the screen' : null]);
  });
}

test('a tab that only marks itself selected still fails the control audit', async ({
  browser,
  page,
}) => {
  const html = `<main><button role="tab" aria-selected="false"
    onclick="this.setAttribute('aria-selected', 'true')">Timeline</button></main>`;
  await page.setContent(html);
  const found = await checkControls(
    browser,
    {},
    await controlsOf(page.getByRole('main')),
    async copy => {
      await copy.setContent(html);
    },
    copy => copy.getByRole('main'),
  );
  expect(found).toEqual(['tab "Timeline" changes nothing on the screen']);
});
