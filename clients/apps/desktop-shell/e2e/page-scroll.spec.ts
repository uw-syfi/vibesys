import {mkdirSync, writeFileSync} from 'node:fs';
import {expect, type Page, test} from '@playwright/test';
import {contactSheetHtml, type SheetEntry} from './checks/contact-sheet.js';
import {open, sidebar} from './world.js';

/**
 * A top-level page's header never scrolls away. On New run, Machines and Settings the window
 * (the document) does not scroll at any size down to 1000 by 700; the header (the wordmark, the
 * title, the actions) stays fully inside the window after the page's body is scrolled to its
 * bottom; and on New run the Start run button stays in view too. The wordmark art is never cut
 * off by its own box. `SCREENS_DIR=<dir>` also writes each page's screenshot, scrolled to the
 * bottom of its body (the app window only), and a contact sheet of them.
 */

const shots = process.env['SCREENS_DIR'] || undefined;
if (shots !== undefined) mkdirSync(shots, {recursive: true});
const sheet: SheetEntry[] = [];

const SIZES = [
  {width: 1000, height: 700},
  {width: 1440, height: 900},
  {width: 1920, height: 1080},
] as const;

type Where = 'New run' | 'Machines' | 'Settings';
const PAGES: readonly Where[] = ['New run', 'Machines', 'Settings'];

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
  if (where !== 'New run') {
    await sidebar(page).getByRole('button', {name: where}).click();
  }
  await expect(
    page.getByRole('heading', {
      name: {'New run': 'New run', Machines: 'Where runs can go', Settings: 'Appearance'}[where],
    }),
  ).toBeVisible();
  if (where === 'New run') {
    // The longest form: Advanced open.
    await page.getByRole('button', {name: 'Advanced'}).click();
    await expect(page.getByLabel('Extra arguments')).toBeVisible();
  }
}

for (const size of SIZES) {
  for (const where of PAGES) {
    test(`${where} keeps its header in view and the window still at ${size.width}x${size.height}`, async ({
      page,
    }) => {
      await page.setViewportSize(size);
      await open(page);
      await goTo(page, where);
      const g = await atBottom(page);
      if (shots !== undefined) {
        const name = `${where.replace(' ', '-').toLowerCase()}__${size.width}x${size.height}`;
        await page.screenshot({path: `${shots}/page-scroll__${name}.png`});
        sheet.push({
          name: `${where} ${size.width}x${size.height}`,
          file: `page-scroll__${name}.png`,
        });
      }
      expect(g.documentOverflow, 'the window scrolls as a whole').toBeLessThanOrEqual(0);
      expect(g.windowScrollY).toBe(0);
      expect(g.artCut ?? 0, 'the wordmark is cut off').toBeLessThanOrEqual(0);
      const outside = g.header.filter(
        box =>
          box.top < 0 || box.left < 0 || box.bottom > g.window.height || box.right > g.window.width,
      );
      expect(outside, 'header parts outside the window').toEqual([]);
      if (where === 'New run') {
        await expect(
          page
            .getByRole('region', {name: 'Ready to start'})
            .getByRole('button', {name: 'Start run'}),
        ).toBeInViewport({ratio: 1});
      }
    });
  }
}

test('at 1000x700 the New run form is longer than its window and scrolls on its own', async ({
  page,
}) => {
  await page.setViewportSize(SIZES[0]);
  await open(page);
  await goTo(page, 'New run');
  expect((await atBottom(page)).scrolls).toBe(true);
});

test('the contact sheet of the pages', async ({browser}) => {
  test.skip(shots === undefined, 'set SCREENS_DIR to write the screenshots and the contact sheet');
  if (shots === undefined) return;
  writeFileSync(`${shots}/page-scroll-sheet.html`, contactSheetHtml(sheet, 3));
  const page = await browser.newPage({viewport: {width: 2400, height: 1200}});
  await page.goto(`file://${shots}/page-scroll-sheet.html`);
  await page.screenshot({path: `${shots}/page-scroll-sheet.png`, fullPage: true});
  await page.close();
});
