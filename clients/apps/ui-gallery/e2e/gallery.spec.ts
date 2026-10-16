import AxeBuilder from '@axe-core/playwright';
import {expect, type Page, test} from '@playwright/test';

/** The faces and weights the type scale uses; each must be the bundled font, not a fallback. */
const FONTS = [
  '400 14px "Schibsted Grotesk"',
  '500 14px "Schibsted Grotesk"',
  '600 24px "Schibsted Grotesk"',
  '400 12.5px "JetBrains Mono"',
  '500 11.5px "JetBrains Mono"',
  '600 22px "JetBrains Mono"',
];

async function themeNames(page: Page): Promise<string[]> {
  await page.goto('./');
  return page.getByRole('navigation', {name: 'Themes'}).getByRole('link').allTextContents();
}

async function open(page: Page, theme: string): Promise<void> {
  await page.goto(`./?theme=${theme}`);
  await expect(page.locator('html')).toHaveAttribute('data-theme', theme);
  const loaded = await page.evaluate(async fonts => {
    await Promise.all(fonts.map(font => document.fonts.load(font)));
    await document.fonts.ready;
    return fonts.filter(font => !document.fonts.check(font));
  }, FONTS);
  expect(loaded, 'fonts that fell back instead of loading').toEqual([]);
}

async function entryNames(page: Page): Promise<string[]> {
  return page
    .locator('[data-gallery-entry]')
    .evaluateAll(entries => entries.map(entry => entry.getAttribute('data-gallery-entry') ?? ''));
}

test('every component looks as agreed, and the page has no axe violation, in every theme', async ({
  page,
}) => {
  const themes = await themeNames(page);
  expect(themes).toHaveLength(6);
  for (const theme of themes) {
    await open(page, theme);
    const axe = await new AxeBuilder({page}).analyze();
    expect.soft(axe.violations, `axe violations in ${theme}`).toEqual([]);
    await expect.soft(page).toHaveScreenshot(`${theme}-Page.png`);
    for (const name of await entryNames(page)) {
      if (name === 'Page') continue;
      const entry = page.locator(`[data-gallery-entry="${name}"]`);
      await expect.soft(entry).toHaveScreenshot(`${theme}-${name}.png`);
    }
    await page.getByRole('button', {name: 'primary', exact: true}).focus();
    await expect
      .soft(page.locator('[data-gallery-entry="Button"]'))
      .toHaveScreenshot(`${theme}-focus-ring.png`);
  }
});

test('a segmented control moves its one tab stop and its choice with the arrow keys', async ({
  page,
}) => {
  await page.goto('./');
  const group = page.getByRole('radiogroup', {name: 'Source (md)'});
  const radios = group.getByRole('radio');
  await radios.first().focus();
  await page.keyboard.press('ArrowRight');
  await expect(radios.nth(1)).toBeChecked();
  await expect(radios.nth(1)).toBeFocused();
  await page.keyboard.press('ArrowLeft');
  await page.keyboard.press('ArrowLeft');
  await expect(radios.nth(2)).toBeChecked();
  await page.keyboard.press('Shift+Tab');
  await page.keyboard.press('Tab');
  await expect(radios.nth(2)).toBeFocused();
});

test('a path input highlights completions with the arrows and takes one with Enter', async ({
  page,
}) => {
  await page.goto('./');
  const input = page.getByRole('combobox', {name: 'Folder'});
  await input.focus();
  await page.keyboard.press('ArrowDown');
  await page.keyboard.press('ArrowDown');
  await expect(page.getByRole('option', {name: /~\/work\/queue/})).toHaveAttribute(
    'aria-selected',
    'true',
  );
  await page.keyboard.press('Enter');
  await expect(input).toHaveValue('~/work/queue');
  await page.keyboard.press('Escape');
  await expect(input).toHaveAttribute('aria-expanded', 'false');
});
