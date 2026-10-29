import {mkdirSync, writeFileSync} from 'node:fs';
import {expect, type Page, test} from '@playwright/test';
import {contactSheetHtml, type SheetEntry} from './checks/contact-sheet.js';
import {revealNav} from './settings-machine.js';
import {open, SEED, sidebar} from './world.js';

/**
 * A top-level page's header never scrolls away. On Machines and Settings the window
 * (the document) does not scroll at any size down to 1000 by 700; the header (the wordmark, the
 * title, the actions) stays fully inside the window after the page's body is scrolled to its
 * bottom. The wordmark art is never cut off by its own box. All of it holds in both
 * the light and the dark theme, and a scroll shade is only ever drawn in theme colors: never a
 * light gradient over a dark page. `SCREENS_DIR=<dir>` also writes each page's screenshot,
 * scrolled to the bottom of its body (the app window only), and a contact sheet of them.
 */

const shots = process.env['SCREENS_DIR'] || undefined;
if (shots !== undefined) mkdirSync(shots, {recursive: true});
const sheet: SheetEntry[] = [];

const SIZES = [
  {width: 1000, height: 700},
  {width: 1440, height: 900},
  {width: 1920, height: 1080},
] as const;

const THEMES = ['paper', 'ink'] as const;
type ThemeName = (typeof THEMES)[number];

/** The seed that opens the app in `theme`: the page follows the system scheme the test emulates. */
function seedFor(theme: ThemeName): typeof SEED {
  return {
    ...SEED,
    theme,
    appearance: {mode: theme === 'ink' ? 'dark' : 'light', light: 'paper', dark: 'ink'},
  };
}

async function openIn(page: Page, theme: ThemeName): Promise<void> {
  await page.emulateMedia({colorScheme: theme === 'ink' ? 'dark' : 'light'});
  await open(page, seedFor(theme));
}

type Where = 'Machines' | 'Settings';
const PAGES: readonly Where[] = ['Machines', 'Settings'];

interface Box {
  readonly top: number;
  readonly bottom: number;
  readonly left: number;
  readonly right: number;
}

interface Geometry {
  /** How far the document could scroll: more than zero is a page that scrolls as a whole. */
  readonly documentOverflow: number;
  readonly windowScrollY: number;
  readonly window: {readonly width: number; readonly height: number};
  /** What the header holds, each visible element's box. */
  readonly header: readonly Box[];
  /** How much of the wordmark art's width its box cuts off (0 when whole), or null with no art. */
  readonly artCut: number | null;
  readonly scrolls: boolean;
}

/** Scrolls the page's body to its end and reads where everything is. */
async function atBottom(page: Page): Promise<Geometry> {
  return page.evaluate(() => {
    const scroller = document.querySelector('.vs-page-scroll');
    if (scroller === null) throw new Error('the page has no scrolling body');
    scroller.scrollTop = scroller.scrollHeight;
    const doc = document.scrollingElement ?? document.documentElement;
    const header = document.querySelector('.vs-page-header');
    if (header === null) throw new Error('the page has no header');
    const boxes = [header, ...header.querySelectorAll('*')].map(element => {
      const {top, bottom, left, right} = element.getBoundingClientRect();
      return {top, bottom, left, right};
    });
    const art = document.querySelector('.vs-wordmark-art');
    return {
      documentOverflow: doc.scrollHeight - doc.clientHeight,
      windowScrollY: window.scrollY,
      window: {width: window.innerWidth, height: window.innerHeight},
      header: boxes,
      artCut: art === null ? null : art.scrollWidth - art.clientWidth,
      scrolls: scroller.scrollHeight > scroller.clientHeight,
    };
  });
}

async function goTo(page: Page, where: Where): Promise<void> {
  if (where === 'Settings') {
    await sidebar(page).getByRole('button', {name: where}).click();
  } else {
    await sidebar(page).getByRole('button', {name: 'Settings'}).click();
    await revealNav(page);
    await page
      .getByRole('navigation', {name: 'Settings sections'})
      .getByRole('button', {name: /Machines/})
      .click();
  }
  await expect(
    page.getByRole('heading', {
      name: {Machines: 'Where runs can go', Settings: 'Appearance'}[where],
    }),
  ).toBeVisible();
}

for (const theme of THEMES) {
  for (const size of SIZES) {
    for (const where of PAGES) {
      test(`${where} keeps its header in view and the window still at ${size.width}x${size.height} (${theme})`, async ({
        page,
      }) => {
        await page.setViewportSize(size);
        await openIn(page, theme);
        await goTo(page, where);
        const g = await atBottom(page);
        if (shots !== undefined) {
          const name = `${where.replace(' ', '-').toLowerCase()}__${size.width}x${size.height}__${theme}`;
          await page.screenshot({path: `${shots}/page-scroll__${name}.png`});
          sheet.push({
            name: `${where} ${size.width}x${size.height} ${theme}`,
            file: `page-scroll__${name}.png`,
          });
        }
        expect(g.documentOverflow, 'the window scrolls as a whole').toBeLessThanOrEqual(0);
        expect(g.windowScrollY).toBe(0);
        expect(g.artCut ?? 0, 'the wordmark is cut off').toBeLessThanOrEqual(0);
        const outside = g.header.filter(
          box =>
            box.top < 0 ||
            box.left < 0 ||
            box.bottom > g.window.height ||
            box.right > g.window.width,
        );
        expect(outside, 'header parts outside the window').toEqual([]);
      });

      test(`${where} draws its scroll shade in ${theme} theme colors at ${size.width}x${size.height}`, async ({
        page,
      }) => {
        await page.setViewportSize(size);
        await openIn(page, theme);
        await goTo(page, where);
        expect(await shadeProblems(page)).toEqual([]);
      });
    }
  }
}

/**
 * What is wrong with the colors the page's scrolling body draws behind its content, if anything:
 * every color in its gradients is a theme token (the page background or the line), and on a dark
 * page none is light.
 */
async function shadeProblems(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    const scroller = document.querySelector('.vs-page-scroll');
    if (scroller === null) return ['the page has no scrolling body'];
    const probe = document.createElement('span');
    document.body.append(probe);
    const resolve = (token: string): string => {
      probe.style.color = `var(${token})`;
      return getComputedStyle(probe).color;
    };
    const tokens = new Map([
      [resolve('--bg'), '--bg'],
      [resolve('--line'), '--line'],
    ]);
    const rgb = (text: string): number[] => (text.match(/[\d.]+/g) ?? []).map(Number);
    const luminance = (text: string): number => {
      const [r = 0, g = 0, b = 0] = rgb(text).map(v => v / 255);
      return 0.2126 * r + 0.7152 * g + 0.0722 * b;
    };
    const dark = luminance(resolve('--bg')) < 0.2;
    probe.remove();
    const colors = getComputedStyle(scroller).backgroundImage.match(/rgba?\([^)]*\)/g) ?? [];
    const problems: string[] = [];
    for (const color of colors) {
      const [, , , alpha = 1] = rgb(color);
      if (alpha === 0) continue;
      if (!tokens.has(color)) problems.push(`${color} is not a theme color`);
      if (dark && luminance(color) > 0.25) problems.push(`${color} is light on a dark page`);
    }
    return problems;
  });
}

test('the contact sheet of the pages', async ({browser}) => {
  test.skip(shots === undefined, 'set SCREENS_DIR to write the screenshots and the contact sheet');
  if (shots === undefined) return;
  writeFileSync(`${shots}/page-scroll-sheet.html`, contactSheetHtml(sheet, 3));
  const page = await browser.newPage({viewport: {width: 2400, height: 1200}});
  await page.goto(`file://${shots}/page-scroll-sheet.html`);
  await page.screenshot({path: `${shots}/page-scroll-sheet.png`, fullPage: true});
  await page.close();
});
