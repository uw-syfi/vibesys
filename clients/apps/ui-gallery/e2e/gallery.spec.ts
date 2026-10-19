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
  await page.mouse.move(0, 0);
  const loaded = await page.evaluate(async fonts => {
    await Promise.all(fonts.map(font => document.fonts.load(font)));
    await document.fonts.ready;
    return fonts.filter(font => !document.fonts.check(font));
  }, FONTS);
  expect(loaded, 'fonts that fell back instead of loading').toEqual([]);
}

/**
 * Shows one entry and hides the rest while `shoot` runs. Entries stack in one page, and what
 * Chromium draws for an entry depends on where it sits there: a change to the height of any entry
 * above it shifted the antialiasing of a few shapes in later baselines, so a baseline update
 * rewrote screens nobody changed. Alone, an entry always sits at the same place and its
 * baseline depends on its own content only.
 */
async function alone(page: Page, name: string, shoot: () => Promise<void>): Promise<void> {
  const setOthers = (display: string) =>
    page.evaluate(
      ([keep, value]) => {
        for (const entry of Array.from(
          document.querySelectorAll<HTMLElement>('[data-gallery-entry]'),
        )) {
          if (entry.dataset['galleryEntry'] !== keep) entry.style.display = value ?? '';
        }
        window.scrollTo(0, 0);
      },
      [name, display],
    );
  await setOthers('none');
  try {
    await shoot();
  } finally {
    await setOthers('');
  }
}

async function entryNames(page: Page): Promise<string[]> {
  return page
    .locator('[data-gallery-entry]')
    .evaluateAll(entries => entries.map(entry => entry.getAttribute('data-gallery-entry') ?? ''));
}

/**
 * The themes the screenshots and axe run in: Ink only, the Vercel-inspired theme the redesign
 * leads with. The other themes stay in the token table, where the contrast test checks every one.
 */
const VALIDATED_THEMES = ['ink'];

test('every component looks as agreed, and the page has no axe violation, in Ink', async ({
  page,
}) => {
  const themes = await themeNames(page);
  expect(themes).toEqual(expect.arrayContaining(VALIDATED_THEMES));
  for (const theme of VALIDATED_THEMES) {
    await open(page, theme);
    const axe = await new AxeBuilder({page}).analyze();
    expect.soft(axe.violations, `axe violations in ${theme}`).toEqual([]);
    await expect.soft(page).toHaveScreenshot(`${theme}-Page.png`);
    for (const name of await entryNames(page)) {
      if (name === 'Page') continue;
      await alone(page, name, () =>
        expect
          .soft(page.locator(`[data-gallery-entry="${name}"]`))
          .toHaveScreenshot(`${theme}-${name}.png`),
      );
    }
    await page.getByRole('button', {name: 'primary', exact: true}).focus();
    await alone(page, 'Button', () =>
      expect
        .soft(page.locator('[data-gallery-entry="Button"]'))
        .toHaveScreenshot(`${theme}-focus-ring.png`),
    );
    await screenshotOverlays(page, theme);
    await screenshotParetoHover(page, theme);
  }
});

/** Hovering a point of the Pareto chart names it and gives its values. */
async function screenshotParetoHover(page: Page, theme: string): Promise<void> {
  await alone(page, 'ParetoChart', async () => {
    const chart = page.locator('[data-gallery-entry="ParetoChart"] .vs-pareto').first();
    await chart.locator('.vs-pareto-point[data-role="frontier"]').nth(2).hover();
    await expect(chart.locator('.vs-pareto-hover-text')).toBeVisible();
    const box = await chart.boundingBox();
    if (box === null) throw new Error('the Pareto chart is not shown');
    await expect.soft(page).toHaveScreenshot(`${theme}-ParetoChart-hover.png`, {clip: box});
    await page.mouse.move(0, 0);
  });
}

/** The states that only show after a press: an open menu, and the modal dialog. */
async function screenshotOverlays(page: Page, theme: string): Promise<void> {
  await alone(page, 'Menu', async () => {
    await page
      .locator('[data-gallery-entry="Menu"]')
      .getByRole('button', {name: 'More actions'})
      .click();
    const entry = await page.locator('[data-gallery-entry="Menu"]').boundingBox();
    const menu = await page.getByRole('menu').boundingBox();
    if (entry === null || menu === null)
      throw new Error('the menu entry or its open menu is not shown');
    const bottom = Math.max(entry.y + entry.height, menu.y + menu.height);
    await page.mouse.move(0, 0);
    await expect.soft(page).toHaveScreenshot(`${theme}-Menu-open.png`, {
      clip: {x: entry.x, y: entry.y, width: entry.width, height: bottom - entry.y},
    });
    await page.keyboard.press('Escape');
  });
  await alone(page, 'Dialog', async () => {
    await page.getByRole('button', {name: 'Open dialog'}).click();
    const dialog = page.getByRole('dialog');
    const axe = await new AxeBuilder({page}).include('dialog').analyze();
    expect.soft(axe.violations, `axe violations in ${theme}'s dialog`).toEqual([]);
    await page.mouse.move(0, 0);
    await expect.soft(page).toHaveScreenshot(`${theme}-Dialog-open.png`);
    await page.keyboard.press('Escape');
    await expect(dialog).toBeHidden();
  });
}

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

test('a menu opens from the keyboard, moves with the arrows, and closes on Esc', async ({page}) => {
  await page.goto('./');
  const trigger = page
    .locator('[data-gallery-entry="Menu"]')
    .getByRole('button', {name: 'More actions'});
  await trigger.focus();
  await page.keyboard.press('ArrowDown');
  const items = page.getByRole('menuitem');
  await expect(items.first()).toBeFocused();
  await page.keyboard.press('ArrowUp');
  await expect(items.last()).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(page.getByRole('menu')).toHaveCount(0);
  await expect(trigger).toBeFocused();
  await trigger.click();
  await page.getByRole('heading', {name: 'Menu', exact: true}).click();
  await expect(page.getByRole('menu')).toHaveCount(0);
});

test('tabs move their one tab stop and the shown panel with the arrow keys', async ({page}) => {
  await page.goto('./');
  const tabs = page.getByRole('tablist', {name: 'Sections'}).getByRole('tab');
  await tabs.first().focus();
  await page.keyboard.press('ArrowLeft');
  await expect(tabs.last()).toBeFocused();
  await expect(tabs.last()).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByRole('tabpanel')).toHaveText('The logs panel.');
  await page.keyboard.press('Home');
  await expect(tabs.first()).toHaveAttribute('aria-selected', 'true');
});

test('a tree follows the tree keyboard model', async ({page}) => {
  await page.goto('./');
  const tree = page.getByRole('tree', {name: 'Workstreams'});
  const row = (name: string) => tree.getByRole('treeitem', {name});
  await row('Spin before parking').focus();
  await page.keyboard.press('ArrowLeft');
  await expect(row('Contention')).toBeFocused();
  await page.keyboard.press('ArrowLeft');
  await expect(row('Contention')).toHaveAttribute('aria-expanded', 'false');
  await expect(row('Pad the counters')).toHaveCount(0);
  await page.keyboard.press('ArrowRight');
  await expect(row('Contention')).toHaveAttribute('aria-expanded', 'true');
  await page.keyboard.press('ArrowRight');
  await expect(row('Pad the counters')).toBeFocused();
  await page.keyboard.press('End');
  await expect(row('Memory layout')).toBeFocused();
  await page.keyboard.press('ArrowRight');
  await page.keyboard.press('ArrowDown');
  await expect(row('Align the slots')).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(row('Align the slots')).toHaveAttribute('aria-selected', 'true');
});

test('a selectable table selects a row with Enter', async ({page}) => {
  await page.goto('./');
  const table = page.getByRole('table', {name: 'Attempts', exact: true});
  const first = table.getByRole('row').nth(1);
  await first.focus();
  await page.keyboard.press('Enter');
  await expect(first).toHaveAttribute('aria-current', 'true');
});

test('a log pane follows the tail until scrolled up, then offers to jump to it', async ({page}) => {
  await page.goto('./');
  const pane = page.getByRole('log', {name: 'Output', exact: true});
  const atEnd = () =>
    pane.evaluate(element => element.scrollHeight - element.scrollTop - element.clientHeight < 2);
  expect(await atEnd()).toBe(true);
  await pane.evaluate(element => {
    element.scrollTop = 0;
  });
  const jump = page.getByRole('button', {name: 'New below'});
  await expect(jump).toBeVisible();
  await jump.click();
  await expect(jump).toHaveCount(0);
  expect(await atEnd()).toBe(true);
});

test('a thread follows its tail until scrolled up, and opens a block from the keyboard', async ({
  page,
}) => {
  await page.goto('./');
  const pane = page.getByRole('list', {name: 'Turn by turn', exact: true});
  await expect(pane.getByText('The batch handoff is slower under contention')).toBeVisible();
  await pane.evaluate(element => {
    element.scrollTop = 0;
  });
  await expect(page.getByRole('button', {name: 'New below'})).toBeVisible();
  const reasoning = pane.getByRole('button', {name: /Reasoning/});
  await reasoning.focus();
  await expect(reasoning).toHaveAttribute('aria-expanded', 'false');
  await page.keyboard.press('Enter');
  await expect(reasoning).toHaveAttribute('aria-expanded', 'true');
  await expect(pane.getByText('Padding costs memory but removes the false sharing.')).toBeVisible();
  await page.getByRole('button', {name: 'New below'}).click();
  await expect(pane.getByText('The batch handoff is slower under contention')).toBeVisible();
});

test('a scrubber steps with the arrow keys and goes back to live', async ({page}) => {
  await page.goto('./');
  const range = page.getByRole('slider', {name: 'Position', exact: true});
  await range.focus();
  await page.keyboard.press('ArrowRight');
  await expect(range).toHaveAttribute('aria-valuetext', 'Step 25 of 40');
  await page.getByRole('button', {name: 'Back to live'}).click();
  await expect(range).toHaveAttribute('aria-valuetext', 'Step 40 of 40');
  await expect(page.getByRole('button', {name: 'Back to live'})).toHaveCount(0);
});
