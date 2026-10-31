import {expect, type Page, test} from '@playwright/test';
import fc from 'fast-check';
import {BAD_SCREENS, CLEAN_CHROME_SCREEN, CLEAN_PAGE, CLEAN_SCREEN} from './checks/bad-dom.js';
import {contactSheetHtml} from './checks/contact-sheet.js';
import {isKnown, KNOWN_FAILURES, type KnownFailure} from './checks/known-failures.js';
import {KnownLedger} from './checks/known-ledger.js';
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

test('a page that keeps the visual spec’s laws passes every check', async ({page}) => {
  await show(page, CLEAN_PAGE);
  expect(await screenCheck(page, IN_PAGE_CHECKS)).toEqual([]);
});

test('a box partly out of sight is not called hollow for what cannot be seen', async ({page}) => {
  await show(
    page,
    `<div style="height:300px"></div><div style="border:1px solid #888;width:440px;height:60px;padding-top:40px">Machine</div>`,
  );
  expect(await screenCheck(page, ['hollow-box'])).toEqual([]);
});

/** Whether `check` holds of a page: `build` draws it from the generated numbers. */
function decides<Case>(
  page: Page,
  check: 'hollow-box' | 'spacing-order' | 'left-edge' | 'heading-depth' | 'title-strip',
  cases: fc.Arbitrary<Case>,
  build: (each: Case) => string,
  fails: (each: Case) => boolean,
): Promise<void> {
  return fc.assert(
    fc.asyncProperty(cases, async each => {
      await show(page, build(each));
      expect((await screenCheck(page, [check])).length > 0).toBe(fails(each));
    }),
    {numRuns: 20},
  );
}

test('property: a box is hollow exactly when its content spans under 40% of it', async ({page}) => {
  await decides(
    page,
    'hollow-box',
    fc.record({box: fc.integer({min: 400, max: 470}), used: fc.integer({min: 20, max: 390})}),
    ({box, used}) =>
      `<div style="border:1px solid #888;box-sizing:border-box;width:${box}px">x<div style="width:${used}px;height:8px;background:#888"></div></div>`,
    ({box, used}) => used < (box - 2) * 0.4,
  );
});

test('property: groups must stand further apart than the rows inside them', async ({page}) => {
  const group = (gap: number): string =>
    `<div style="display:flex;flex-direction:column;gap:${gap}px"><span>Name</span><span>Host</span></div>`;
  await decides(
    page,
    'spacing-order',
    fc.record({inside: fc.integer({min: 1, max: 24}), between: fc.integer({min: 1, max: 24})}),
    ({inside, between}) =>
      `<div style="display:flex;flex-direction:column;gap:${between}px">${group(inside)}${group(inside)}</div>`,
    ({inside, between}) => between <= inside,
  );
});

test('property: a back control off the content’s left edge fails past a pixel', async ({page}) => {
  await decides(
    page,
    'left-edge',
    fc.integer({min: 0, max: 40}),
    inset =>
      `<div class="vs-page-content"><div class="vs-page-header" style="display:flex;flex-direction:column"><button aria-label="Back" style="all:unset;display:flex;padding-left:${inset}px"><svg width="16" height="16"><path d="M0 3h16v10H0z"/></svg></button><h1 style="font-size:24px;line-height:28px;margin:0">Logins</h1></div><div class="vs-page-scroll"><div class="vs-page-body"><div>Machine</div></div></div></div>`,
    inset => inset > 1,
  );
});

test('property: nothing under a title is as large as the title', async ({page}) => {
  await decides(
    page,
    'heading-depth',
    fc.record({title: fc.integer({min: 12, max: 32}), child: fc.integer({min: 10, max: 36})}),
    ({title, child}) =>
      `<div class="vs-page-content"><div class="vs-page-header"><h1 style="font-size:${title}px;margin:0">Logins</h1></div><div class="vs-page-scroll"><div class="vs-page-body" style="font-size:${child}px"><div>Machine</div></div></div></div>`,
    ({title, child}) => child >= title,
  );
});

test('property: a window’s controls strip is wasted exactly when nothing is drawn in its first 24 px', async ({
  page,
}) => {
  await decides(
    page,
    'title-strip',
    fc.record({
      controls: fc.integer({min: 0, max: 60}),
      start: fc.integer({min: 0, max: 80}),
      toggle: fc.boolean(),
    }),
    ({controls, start, toggle}) =>
      `<style>:root{--chrome-inset-height:${controls}px}</style><div style="display:flow-root;height:${start}px">${toggle ? '<button style="margin:2px 90px">Menu</button>' : ''}</div><div style="width:100px;height:8px;background:#888"></div>`,
    // The first box sits `start` px down, or under a toggle that shares the strip with the controls.
    ({controls, start, toggle}) => controls > 0 && start > 24 && !toggle,
  );
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

test('a chip, a toast, a tile, a log and a status word beside its control are not status rows', async ({
  page,
}) => {
  await show(
    page,
    `<main style="display:block;width:440px">
      <span class="vs-chip" data-tone="warn" role="status">Needs an update</span>
      <section class="vs-toast-stack" aria-live="polite"><div class="vs-toast" role="alert">Could not resume the run</div></section>
      <div class="vs-overview-tile" role="status">3 need you</div>
      <div class="vs-log"><div role="log"><span class="vs-text" data-tone="err" style="display:block">error: linker not found</span></div></div>
      <div style="display:flex;gap:8px"><span class="vs-text" data-tone="err">Unavailable</span><button>Retry history</button></div>
    </main>`,
  );
  expect(await screenCheck(page, ['status-row'])).toEqual([]);
});

/**
 * What a review of the check found it flagged and should not: it measures the words, not the box.
 * Each of these has a box as wide as the pane and little to say in it.
 */
test('a stretched figure, a lone toned word, a live wrapper and a chip with its detail are not status rows', async ({
  page,
}) => {
  await show(
    page,
    `<main style="display:block;width:440px">
      <div style="display:flex;flex-direction:column"><span class="vs-text" data-tone="ok">1.42×</span></div>
      <div style="display:flex;flex-direction:column"><span class="vs-text" data-tone="warn">No tasks</span></div>
      <div aria-live="polite"><span class="vs-chip" data-tone="ok">Ready</span> <button>Start run</button></div>
      <div role="alert"><div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center"><span class="vs-chip" data-tone="err">The workspace is not usable</span><span>the workspace /scratch/vibesys is missing or not writable, and the path it names is not a directory either, so check the mount and try the test again</span></div></div>
    </main>`,
  );
  expect(await screenCheck(page, ['status-row'])).toEqual([]);
});

test('a status row is found by its words, tinted or not, and a hidden or narrow one is not', async ({
  page,
}) => {
  await show(
    page,
    `<main style="display:block;width:440px">
      <div role="alert">Could not read the runs of this machine, try again</div>
      <div role="alert" hidden>Never drawn, though it is long enough to count if it were</div>
      <div role="alert" style="width:100px">Narrow</div>
    </main>`,
  );
  const found = await screenCheck(page, ['status-row']);
  expect(found).toHaveLength(1);
  expect(found[0]?.message).toMatch(
    /^a status row \d+% of the main pane wide \(alert\): "Could not read the runs of this machine,"$/,
  );
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
    suite: 'run-matrix',
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

test('a paragraph at the line cap passes pane-fill however wide its pane is', async ({page}) => {
  await page.setViewportSize({width: 1800, height: 320});
  const text = 'The judge read the candidate source and found that it matches the outcome. '.repeat(
    4,
  );
  await show(
    page,
    `<div class="vs-pane-body" style="width:1600px;font:14px/20px monospace"><div style="max-width:735px">${text}</div></div>`,
  );
  expect(await screenCheck(page, ['pane-fill'])).toEqual([]);
});

test('a ledger names a suite’s entries no screen showed, and leaves other suites’ alone', () => {
  const failure = (id: string, suite: KnownFailure['suite'], text: RegExp): KnownFailure => ({
    id,
    suite,
    check: 'hollow-box',
    message: text,
    issue: 'https://github.com/uw-syfi/vibesys/issues/1',
  });
  const seen = failure('seen', 'settings', /wide/);
  const fixed = failure('fixed', 'settings', /narrow/);
  const elsewhere = failure('elsewhere', 'usage', /narrow/);
  const ledger = new KnownLedger('settings', [seen, fixed, elsewhere]);
  const wide = {check: 'hollow-box', message: 'a wide box', where: 'div'} as const;
  const narrow = {check: 'hollow-box', message: 'a tall box', where: 'div'} as const;
  expect(ledger.unexplained('settings/logins/1440', [wide, narrow])).toEqual([
    'settings/logins/1440: [hollow-box] a tall box (div)',
  ]);
  expect(ledger.screens).toBe(1);
  expect(ledger.stale()).toEqual(['fixed']);
});
