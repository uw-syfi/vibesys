import {expect, test} from '@playwright/test';
import {
  CHECK_VIEWPORTS,
  expectAccessibleShell,
  expectBundledFontsReady,
  expectNoHorizontalOverflow,
  expectPageColumnScrollable,
  expectSingleDeclaredPageScroller,
  horizontalOverflow,
  observePageIssues,
  verticalScrollReport,
  waitForFiniteAnimations,
} from './checks.js';
import {FIXTURE_SCREENS, openFixture} from './journey.js';

for (const viewport of CHECK_VIEWPORTS) {
  test(`rail destinations keep their semantics and layout at ${viewport.width}x${viewport.height}`, async ({
    page,
  }) => {
    const issues = observePageIssues(page);
    await page.setViewportSize(viewport);
    await openFixture(page);

    for (const screen of FIXTURE_SCREENS) {
      await screen.open(page);
      await expectBundledFontsReady(page);
      await waitForFiniteAnimations(page);
      await expectAccessibleShell(page, screen);
      await expectSingleDeclaredPageScroller(page);
      await expectNoHorizontalOverflow(page);
      if (screen.slug === 'runs') await expectPageColumnScrollable(page);
    }

    issues.expectNone(`${viewport.name} fixture journey`);
  });
}

test('the layout readers expose an undeclared scroller and window overflow', async ({page}) => {
  await page.setViewportSize({width: 480, height: 600});
  await page.setContent(`
    <main>
      <div class="vs-page-content">
        <div data-scroll="column" style="overflow: hidden auto; height: 100px">
          <div style="height: 200px">declared</div>
        </div>
      </div>
      <div id="undeclared" style="overflow-y: auto; width: 900px; height: 100px">
        <div style="height: 200px">undeclared</div>
      </div>
    </main>
  `);

  const vertical = await verticalScrollReport(page);
  expect(vertical.pageRegions).toEqual(['div']);
  expect(vertical.undeclared).toContain('div#undeclared');
  expect(await horizontalOverflow(page)).not.toEqual([]);
});
