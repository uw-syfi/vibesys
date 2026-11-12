import {mkdir} from 'node:fs/promises';
import {resolve} from 'node:path';
import {expect, test} from '@playwright/test';
import {
  APPROVAL_VIEWPORTS,
  expectAccessibleShell,
  expectBundledFontsReady,
  expectNoHorizontalOverflow,
  expectSingleDeclaredPageScroller,
  observePageIssues,
  waitForFiniteAnimations,
} from './checks.js';
import {type FixtureCapture, writeCaptureMarkdown, writeContactSheet} from './contact-sheet.js';
import {FIXTURE_SCREENS, openFixture} from './journey.js';

const configured = process.env['VIBESYS_DESKTOP_FIXTURE_CAPTURE_DIR'];

test('@capture writes full-size approval screens and one contact sheet', async ({page}) => {
  const root = resolve(process.cwd(), configured || '../../../artifacts/desktop-fixture');
  await mkdir(root, {recursive: true});
  const issues = observePageIssues(page);
  const captures: FixtureCapture[] = [];

  for (const viewport of APPROVAL_VIEWPORTS) {
    await page.setViewportSize(viewport);
    await openFixture(page);
    for (const screen of FIXTURE_SCREENS) {
      await screen.open(page);
      await expectBundledFontsReady(page);
      await waitForFiniteAnimations(page);
      await expectAccessibleShell(page, screen);
      await expectSingleDeclaredPageScroller(page);
      await expectNoHorizontalOverflow(page);
      const fileName = `${screen.slug}-${viewport.width}x${viewport.height}.png`;
      const png = await page.screenshot({
        path: resolve(root, fileName),
        animations: 'disabled',
        caret: 'hide',
      });
      captures.push({screen: screen.heading, viewport, fileName, png});
    }
  }

  issues.expectNone('approval capture journey');
  expect(captures).toHaveLength(APPROVAL_VIEWPORTS.length * FIXTURE_SCREENS.length);
  await writeContactSheet(page, captures, resolve(root, 'contact-sheet.png'));
  await writeCaptureMarkdown(captures, resolve(root, 'screens.md'));
});
