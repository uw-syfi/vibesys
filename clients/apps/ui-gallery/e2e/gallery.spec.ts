import AxeBuilder from '@axe-core/playwright';
import {expect, type Page, test} from '@playwright/test';
import {foreignFontMessage, foreignFontUses} from '../../../tools/e2e/bundled-fonts.mjs';

/** The faces and weights components use; each must be the bundled font, not a fallback. */
const FONTS = [
  '400 14px "Schibsted Grotesk"',
  '500 14px "Schibsted Grotesk"',
  '600 12px "Schibsted Grotesk"',
  '400 12.5px "JetBrains Mono"',
  '500 12.5px "JetBrains Mono"',
  '600 12.5px "JetBrains Mono"',
  '700 30px "JetBrains Mono"',
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

async function entryNames(page: Page): Promise<string[]> {
  return page
    .locator('[data-gallery-entry]')
    .evaluateAll(entries => entries.map(entry => entry.getAttribute('data-gallery-entry') ?? ''));
}

/** What a page reports when it is wrong: uncaught exceptions and console errors. */
function watchErrors(page: Page): string[] {
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(`uncaught: ${error.message}`));
  page.on('console', message => {
    if (message.type() === 'error') errors.push(`console: ${message.text()}`);
  });
  return errors;
}

test('every entry draws, without an error or a stray scrollbar, in every theme', async ({page}) => {
  const errors = watchErrors(page);
  for (const theme of await themeNames(page)) {
    await open(page, theme);
    const names = await entryNames(page);
    expect(names.length, `${theme}: entries drawn`).toBeGreaterThan(0);
    expect(new Set(names).size, `${theme}: entry names are unique`).toBe(names.length);
    const empty = await page.locator('[data-gallery-entry]').evaluateAll(entries =>
      entries
        .filter(entry => {
          const box = entry.getBoundingClientRect();
          return box.width === 0 || box.height === 0;
        })
        .map(entry => entry.getAttribute('data-gallery-entry')),
    );
    expect(empty, `${theme}: entries with no size`).toEqual([]);
    const overflowing = await page.evaluate(() =>
      Array.from(document.querySelectorAll<HTMLElement>('[data-gallery-entry] *'))
        .filter(element => {
          const overflow = getComputedStyle(element).overflowX;
          return (
            (overflow === 'auto' || overflow === 'scroll') &&
            element.scrollWidth > element.clientWidth + 1
          );
        })
        .map(
          element =>
            `${element.closest('[data-gallery-entry]')?.getAttribute('data-gallery-entry')} ${element.className}`,
        ),
    );
    expect(overflowing, `${theme}: horizontally scrolling regions`).toEqual([]);
  }
  expect(errors, 'errors the page reported').toEqual([]);
});

test('the page, an open menu and the open dialog have no axe violation, in every theme', async ({
  page,
}) => {
  const errors = watchErrors(page);
  for (const theme of await themeNames(page)) {
    await open(page, theme);
    const axe = await new AxeBuilder({page}).analyze();
    expect.soft(axe.violations, `axe violations in ${theme}`).toEqual([]);
    await openOverlaysUnderAxe(page, theme);
  }
  expect(errors, 'errors the page reported').toEqual([]);
});

/** The states that only show after a press: an open menu, and the modal dialog. */
async function openOverlaysUnderAxe(page: Page, theme: string): Promise<void> {
  await page
    .locator('[data-gallery-entry="Menu"]')
    .getByRole('button', {name: 'More actions'})
    .click();
  await expect(page.getByRole('menu')).toBeVisible();
  const menu = await new AxeBuilder({page}).include('[role="menu"]').analyze();
  expect.soft(menu.violations, `axe violations in ${theme}'s open menu`).toEqual([]);
  await page.keyboard.press('Escape');
  await page.getByRole('button', {name: 'Open dialog'}).click();
  const dialog = page.getByRole('dialog');
  const axe = await new AxeBuilder({page}).include('dialog').analyze();
  expect.soft(axe.violations, `axe violations in ${theme}'s dialog`).toEqual([]);
  await page.keyboard.press('Escape');
  await expect(dialog).toBeHidden();
}

test('a focused button shows a focus ring that its resting state lacks, in every theme', async ({
  page,
}) => {
  for (const theme of await themeNames(page)) {
    await open(page, theme);
    const button = page
      .locator('[data-gallery-entry="Button"]')
      .getByRole('button', {name: 'primary', exact: true});
    const ring = () =>
      button.evaluate(element => {
        const style = getComputedStyle(element);
        return {
          outline: `${style.outlineStyle} ${style.outlineWidth} ${style.outlineColor}`,
          shadow: style.boxShadow,
        };
      });
    const resting = await ring();
    await button.focus();
    const focused = await ring();
    expect(focused, `${theme}: focus ring`).not.toEqual(resting);
  }
});

test('every glyph the gallery draws comes from a bundled font, none from the machine', async ({
  page,
}) => {
  await open(page, 'ink');
  const foreign = await foreignFontUses(page);
  expect(foreign, foreignFontMessage(foreign)).toEqual([]);
});

test('hovering a point of the Pareto chart names it and gives its values', async ({page}) => {
  await open(page, 'ink');
  const chart = page.locator('[data-gallery-entry="ParetoChart"] .vs-pareto').first();
  await chart.locator('.vs-pareto-point[data-role="frontier"]').nth(2).hover();
  await expect(chart.locator('.vs-pareto-hover-text')).toBeVisible();
  await expect(chart.locator('.vs-pareto-hover-text')).not.toHaveText('');
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
  const owner = page.locator('[data-gallery-entry="LogPane"] .vs-log');
  const ownerBefore = await owner.boundingBox();
  const atEnd = () =>
    pane.evaluate(element => element.scrollHeight - element.scrollTop - element.clientHeight < 2);
  expect(await atEnd()).toBe(true);
  await pane.evaluate(element => {
    element.scrollTop = 0;
  });
  const jump = page.getByRole('button', {name: 'New below'});
  await expect(jump).toBeVisible();
  const paneBox = await pane.boundingBox();
  const jumpBox = await jump.boundingBox();
  const ownerAfter = await owner.boundingBox();
  expect(paneBox).not.toBeNull();
  expect(jumpBox).not.toBeNull();
  expect(ownerAfter).toEqual(ownerBefore);
  expect(jumpBox?.y).toBeGreaterThanOrEqual((paneBox?.y ?? 0) + (paneBox?.height ?? 0));
  await jump.click();
  await expect(jump).toHaveCount(0);
  expect(await atEnd()).toBe(true);
});

test('a thread follows its tail until scrolled up, and opens a block from the keyboard', async ({
  page,
}) => {
  await page.goto('./');
  const pane = page.getByRole('list', {name: 'Turn by turn', exact: true});
  const owner = page.locator('[data-gallery-entry="Thread"] .vs-thread');
  const ownerBefore = await owner.boundingBox();
  await expect(pane.getByText('The batch handoff is slower under contention')).toBeVisible();
  await pane.evaluate(element => {
    element.scrollTop = 0;
  });
  const jump = page.getByRole('button', {name: 'New below'});
  await expect(jump).toBeVisible();
  const paneBox = await pane.boundingBox();
  const jumpBox = await jump.boundingBox();
  const ownerAfter = await owner.boundingBox();
  expect(paneBox).not.toBeNull();
  expect(jumpBox).not.toBeNull();
  expect(ownerAfter).toEqual(ownerBefore);
  expect(jumpBox?.y).toBeGreaterThanOrEqual((paneBox?.y ?? 0) + (paneBox?.height ?? 0));
  const reasoning = pane.getByRole('button', {name: /Reasoning/});
  await reasoning.focus();
  await expect(reasoning).toHaveAttribute('aria-expanded', 'false');
  await page.keyboard.press('Enter');
  await expect(reasoning).toHaveAttribute('aria-expanded', 'true');
  await expect(pane.getByText('Padding costs memory but removes the false sharing.')).toBeVisible();
  await jump.click();
  await expect(pane.getByText('The batch handoff is slower under contention')).toBeVisible();
});

test('every followed prose viewport keeps its tail control outside the content', async ({page}) => {
  await page.setViewportSize({width: 640, height: 600});
  await page.goto('./');
  for (const view of [
    {entry: 'Conversation', label: 'A conversation', owner: '.vs-conversation'},
    {entry: 'Narrative', label: 'Turn', owner: '.vs-narrative-wrap'},
  ]) {
    const entry = page.locator(`[data-gallery-entry="${view.entry}"]`);
    const pane = entry.locator(`[aria-label="${view.label}"]`);
    const owner = entry.locator(view.owner);
    const ownerBefore = await owner.boundingBox();
    await pane.evaluate(element => {
      element.scrollTop = 0;
    });
    const jump = entry.getByRole('button', {name: 'New below'});
    await expect(jump).toBeVisible();
    const paneBox = await pane.boundingBox();
    const jumpBox = await jump.boundingBox();
    expect(await owner.boundingBox(), `${view.entry}: owner stays put`).toEqual(ownerBefore);
    expect(jumpBox?.y, `${view.entry}: control starts after viewport`).toBeGreaterThanOrEqual(
      (paneBox?.y ?? 0) + (paneBox?.height ?? 0),
    );
    await jump.click();
    await expect(jump).toHaveCount(0);
  }
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

/** Waits for the page's finite CSS transitions to end; the runner's timeout only guards a hang. */
async function settled(page: Page): Promise<void> {
  await page.evaluate(() =>
    Promise.all(
      document
        .getAnimations()
        .filter(animation => animation.effect?.getComputedTiming().iterations !== Infinity)
        .map(animation => animation.finished),
    ),
  );
}

test('disabled buttons keep their appearance when hovered', async ({page}) => {
  await open(page, 'ink');
  const buttons = page
    .locator('[data-gallery-entry="Button"]')
    .getByRole('button', {name: 'Disabled', exact: true});
  await expect(buttons).toHaveCount(4);
  for (const button of await buttons.all()) {
    const appearance = () =>
      button.evaluate(element => {
        const style = getComputedStyle(element);
        return {
          color: style.color,
          background: style.backgroundColor,
          border: style.borderColor,
          opacity: style.opacity,
        };
      });
    await page.mouse.move(0, 0);
    await settled(page);
    const idle = await appearance();
    await button.hover();
    await settled(page);
    expect(await appearance()).toEqual(idle);
  }
});

test('the section navigation of a narrow page has one highlight per item, apart, on the content edge', async ({
  page,
}) => {
  await page.setViewportSize({width: 480, height: 800});
  for (const theme of await themeNames(page)) {
    await open(page, theme);
    const entry = page.locator('[data-gallery-entry="SectionLayout"]');
    const second = entry.getByRole('button', {name: 'Machines'});
    await second.scrollIntoViewIfNeeded();
    await second.hover();
    const read = () =>
      entry.evaluate(element => {
        const box = (target: Element) => target.getBoundingClientRect();
        const paint = (target: Element) => getComputedStyle(target).backgroundColor;
        const nav = element.querySelector('.vs-section-nav');
        const content = element.querySelector('.vs-section-content');
        if (nav === null || content === null) throw new Error('the section layout is not drawn');
        const items = Array.from(nav.querySelectorAll('.vs-sidebar-item')).map(item => ({
          rect: box(item).toJSON(),
          itemPaint: paint(item),
          linkPaint: paint(item.querySelector('.vs-sidebar-link') ?? item),
        }));
        return {
          items,
          navLeft: box(nav).left,
          contentLeft: box(content).left,
          gap: Number.parseFloat(getComputedStyle(nav).columnGap),
        };
      });
    const drawn = await read();
    expect(drawn.items).toHaveLength(3);
    // One shape per item: the press target inside it paints nothing, hovered or not.
    for (const item of drawn.items)
      expect(item.linkPaint, `${theme}: link paint`).toBe('rgba(0, 0, 0, 0)');
    // The hovered item is the one highlight; the others in the row are not painted.
    expect(drawn.items[1]?.itemPaint).not.toBe('rgba(0, 0, 0, 0)');
    // Highlights never touch: neighbours in a row, or in a column, are a group gap apart.
    expect(drawn.gap).toBeGreaterThanOrEqual(8);
    drawn.items.slice(1).forEach((item, index) => {
      const before = drawn.items[index]?.rect;
      if (before === undefined) return;
      const apart =
        item.rect.top >= before.bottom
          ? item.rect.top - before.bottom
          : item.rect.left - before.right;
      expect(apart, `${theme}: items ${index} and ${index + 1}`).toBeGreaterThanOrEqual(
        drawn.gap - 0.5,
      );
    });
    // The navigation and its first item share the content's left edge.
    expect(Math.abs(drawn.navLeft - drawn.contentLeft), `${theme}: nav edge`).toBeLessThan(0.5);
    expect(
      Math.abs((drawn.items[0]?.rect.left ?? 0) - drawn.contentLeft),
      `${theme}: item edge`,
    ).toBeLessThan(0.5);
  }
});

test('the rail holds Settings at its foot at every window height, never touching the items above', async ({
  page,
}) => {
  await page.setViewportSize({width: 1100, height: 600});
  await open(page, 'ink');
  const entry = page.locator('[data-gallery-entry="RailFrame"]');
  await entry.scrollIntoViewIfNeeded();
  const read = (frameHeight: string) =>
    entry.evaluate((element, height) => {
      const frame = element.querySelector<HTMLElement>('.vs-window-frame');
      const rail = element.querySelector<HTMLElement>('.vs-rail');
      if (frame === null || rail === null) throw new Error('the rail is not drawn');
      frame.style.height = height;
      const box = (target: Element) => target.getBoundingClientRect();
      const named = (name: string) => {
        const found = Array.from(rail.querySelectorAll('.vs-rail-item')).find(
          item => item.textContent === name,
        );
        if (found === undefined) throw new Error(`the rail has no ${name}`);
        return box(found);
      };
      const style = getComputedStyle(rail);
      return {
        names: Array.from(rail.querySelectorAll('.vs-rail-item')).map(item => item.textContent),
        rail: box(rail).toJSON(),
        settings: named('Settings').toJSON(),
        machines: named('Machines').toJSON(),
        runs: named('Runs').toJSON(),
        cost: named('Cost').toJSON(),
        padBottom: Number.parseFloat(style.paddingBottom),
        gap: Number.parseFloat(
          getComputedStyle(rail.querySelector('.vs-rail-group') ?? rail).rowGap,
        ),
        scrolls: rail.scrollHeight > rail.clientHeight,
      };
    }, frameHeight);
  // A 600 px window: Settings sits on the rail's bottom padding, the top group on its own gap.
  const tall = await read('100vh');
  expect(tall.names).toEqual(['Runs', 'Cost', 'Machines', 'Settings']);
  expect(tall.rail.height).toBeCloseTo(600, 0);
  expect(tall.rail.bottom - tall.padBottom - tall.settings.bottom).toBeLessThan(0.5);
  expect(tall.gap).toBeGreaterThanOrEqual(8);
  // The top group keeps the shared gap between its items.
  expect(tall.cost.top - tall.runs.bottom).toBeGreaterThanOrEqual(tall.gap - 0.5);
  expect(tall.machines.top - tall.cost.bottom).toBeGreaterThanOrEqual(tall.gap - 0.5);
  expect(tall.settings.top - tall.machines.bottom).toBeGreaterThanOrEqual(tall.gap - 0.5);
  expect(tall.scrolls).toBe(false);
  // Too short for both groups: they stay a step apart and the rail scrolls, so nothing overlaps.
  const short = await read('120px');
  expect(short.settings.top - short.machines.bottom).toBeGreaterThanOrEqual(12 - 0.5);
  expect(short.scrolls).toBe(true);
});
