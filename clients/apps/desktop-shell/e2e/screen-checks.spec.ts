import {expect, type Page, test} from '@playwright/test';
import {BAD_SCREENS, CLEAN_CHROME_SCREEN, CLEAN_SCREEN} from './checks/bad-dom.js';
import {contactSheetHtml} from './checks/contact-sheet.js';
import {isKnown, KNOWN_FAILURES, type KnownFailure} from './checks/known-failures.js';
import {IN_PAGE_CHECKS, pressMovesWindow, report, screenCheck} from './checks/screen-check.js';
import {settled} from './checks/settle.js';
import {anchorsOf, RUN_ANCHORS, stabilityViolations} from './checks/stability.js';

/**
 * The checks check themselves: each one fails on a screen that is known to be wrong (the defects
 * users reported), and none fails on a clean one. Pages are small and use the browser's default
 * fonts at a fixed size, so the results do not depend on the app or on the machine.
 */

test.use({viewport: {width: 480, height: 320}});

async function show(page: Page, html: string): Promise<void> {
  await page.setContent(`<!doctype html><html><body style="margin:0">${html}</body></html>`);
}

test('a clean screen passes every check', async ({page}) => {
  await show(page, CLEAN_SCREEN);
  // The bundled-font check needs the bundled faces; `bundled-font.spec.ts` loads them.
  expect(await screenCheck(page, IN_PAGE_CHECKS)).toEqual([]);
});

test('a page laid out around the window controls passes every check, and its blank space moves the window', async ({
  page,
}) => {
  await show(page, CLEAN_CHROME_SCREEN);
  expect(await screenCheck(page, IN_PAGE_CHECKS)).toEqual([]);
  // The column's blank space and the top of the page drag; a control in the column, and the page's own space, do not.
  expect(
    await pressMovesWindow(page, [
      [40, 200],
      [10, 10],
      [200, 4],
      [20, 55],
      [200, 100],
    ]),
  ).toEqual([true, true, true, false, false]);
});

for (const bad of BAD_SCREENS) {
  test(`${bad.check} fails on ${bad.name}`, async ({page}) => {
    await show(page, bad.html);
    const violations = await screenCheck(page, IN_PAGE_CHECKS);
    const hits = violations.filter(each => each.check === bad.check);
    expect(
      hits.map(each => each.message),
      `${bad.check} found nothing`,
    ).not.toEqual([]);
    expect(hits.some(each => bad.message.test(each.message))).toBe(true);
  });
}

const TONES = '<style>:root{--chip-warn-bg:#f3e6c8;--banner-err-bg:#f9e4e1;--warn:#925500}</style>';

test('a tinted chip, toast and attention tile are not banners, and a plain strip is not tinted', async ({
  page,
}) => {
  await show(
    page,
    `${TONES}<main style="display:block;width:440px">
      <span class="vs-chip" style="display:block;background:var(--chip-warn-bg)">Needs an update</span>
      <div class="vs-toast" style="background:var(--banner-err-bg)">Could not resume the run</div>
      <div class="vs-overview-tile" style="background:var(--banner-err-bg)">3 need you</div>
      <div style="background:#ffffff">Ready to start <button>Start</button></div>
    </main>`,
  );
  expect(await screenCheck(page, ['banner'])).toEqual([]);
});

test('a banner is found by its color, whatever it is called or where it sits', async ({page}) => {
  await show(
    page,
    `${TONES}<header class="status-line" style="display:block;width:440px;background:var(--chip-warn-bg)">Check the host</header>
    <main style="display:block;width:440px"><p>Run</p></main>`,
  );
  const found = await screenCheck(page, ['banner']);
  expect(found.map(each => each.message)).toEqual([
    'a strip 100% of the main pane wide is tinted by a status color: "Check the host"',
  ]);
});

const CHART = `<div id="row" style="position:relative;height:90px">
  <svg id="chart" role="group" aria-label="Metric of each measured idea over the run, with the best so far" width="240" height="80"><circle cx="40" cy="40" r="6" fill="red"/></svg>
  <input id="bar" type="range" aria-label="Run position" style="position:absolute;left:260px;top:10px"/>
</div>`;

test('settled waits for subpixel movement to finish', async ({page}) => {
  await show(page, '<div id="moving" style="width:20px;height:20px"></div>');
  await page.evaluate(() => {
    const moving = document.getElementById('moving');
    let frame = 0;
    const advance = (): void => {
      frame += 1;
      if (moving) moving.style.transform = `translateX(${frame * 0.11}px)`;
      if (frame < 30) requestAnimationFrame(advance);
    };
    requestAnimationFrame(advance);
  });
  await settled(page);
  const x = await page.locator('#moving').evaluate(element => element.getBoundingClientRect().x);
  expect(x).toBeCloseTo(3.3, 1);
});

test('a chart replaced by a copy on navigation is a remount', async ({page}) => {
  await show(page, CHART);
  const before = await anchorsOf(page, RUN_ANCHORS);
  expect(await stabilityViolations(page, 'idea', before, RUN_ANCHORS)).toEqual([]);
  await page.evaluate(() => {
    const chart = document.getElementById('chart');
    chart?.replaceWith(chart.cloneNode(true));
  });
  const moved = await stabilityViolations(page, 'idea', before, RUN_ANCHORS);
  expect(moved.map(each => each.message)).toEqual([
    'the performance chart was replaced by a new element (a remount) at "idea"',
  ]);
});

test('a chart that moves or vanishes on navigation is reported', async ({page}) => {
  await show(page, CHART);
  const before = await anchorsOf(page, RUN_ANCHORS);
  await page.evaluate(() => {
    const row = document.getElementById('row');
    if (row) row.style.paddingTop = '6px';
  });
  const moved = await stabilityViolations(page, 'step', before, RUN_ANCHORS);
  expect(moved.map(each => each.message)).toEqual([
    'the performance chart changed its y from 0 to 6 at "step"',
  ]);
  await page.evaluate(() => document.getElementById('chart')?.remove());
  const gone = await stabilityViolations(page, 'item', before, RUN_ANCHORS);
  expect(gone.map(each => each.message)).toContain('the performance chart is gone at "item"');
});

test('the known failures are listed with an issue each, and a report separates them', () => {
  for (const failure of KNOWN_FAILURES) {
    expect(failure.issue, failure.id).toMatch(
      /^https:\/\/github\.com\/uw-syfi\/vibesys\/issues\/\d+$/,
    );
  }
  const failure: KnownFailure = {
    id: 'example',
    check: 'raw-text',
    message: /snake_case identifier "total_ops_per_sec"/,
    screens: /\/run\//,
    issue: 'https://github.com/uw-syfi/vibesys/issues/1',
  };
  const raw = {
    check: 'raw-text',
    message: 'raw text shown: snake_case identifier "total_ops_per_sec" in "x"',
    where: 'span',
  } as const;
  const other = {...raw, message: 'raw text shown: snake_case identifier "other_thing" in "x"'};
  expect(isKnown(failure, 'a/run/b', raw)).toBe(true);
  expect(isKnown(failure, 'a/idea/b', raw)).toBe(false);
  const result = report('a/run/b', [raw, other], [failure]);
  expect(result.known.map(each => each.violation)).toEqual([raw]);
  expect(result.unexplained).toEqual([other]);
});

test('the contact sheet names and shows every screen, escaped', () => {
  const html = contactSheetHtml(
    [
      {name: 'queue-rs-live/run/<sidebar>/1440', file: 'a.png'},
      {name: 'finished-run/idea/sidebar/1000', file: 'b.png'},
    ],
    2,
  );
  expect(html).toContain('src="a.png"');
  expect(html).toContain('src="b.png"');
  expect(html).toContain('queue-rs-live/run/&lt;sidebar&gt;/1440');
  expect(html).not.toContain('<sidebar>');
});

for (const broken of [false, true]) {
  test(`a modal surface ${broken ? 'still reports its own overlap and focus-ring defects' : 'may cover its background'}`, async ({
    page,
  }) => {
    await show(
      page,
      `
      <span style="position:absolute;left:8px;top:8px">Background title</span>
      <span style="position:absolute;left:8px;top:38px">Background field</span>
      <dialog id="modal" style="margin:0;left:0;top:0;padding:8px;border:0;width:220px;height:100px">
        <span>Message</span>
        <input aria-label="Message" autofocus style="position:absolute;left:8px;top:40px;width:160px;height:20px;outline:4px solid blue"/>
        ${broken ? '<span style="position:absolute;left:8px;top:8px">Overlapping title</span><span style="position:absolute;left:8px;top:36px">Crowded field</span>' : ''}
      </dialog>
      <script>document.getElementById('modal').showModal();</script>
    `,
    );
    const violations = await screenCheck(page, ['overlap', 'focus-ring']);
    if (broken) {
      expect(violations.some(each => each.check === 'overlap')).toBe(true);
      expect(violations.some(each => each.check === 'focus-ring')).toBe(true);
    } else {
      expect(violations).toEqual([]);
    }
  });
}
