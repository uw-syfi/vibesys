/**
 * Screenshots of the run window for review: CAPTURE_DIR=<dir> pnpm --filter @vibesys/web exec
 * playwright test screens. Each screen is written at 1440 and 1024, dark and light, as
 * <name>-<width>-<theme>.png. Without CAPTURE_DIR the spec is skipped (CI runs app.spec only).
 */
import {join} from 'node:path';
import {type Page, test} from '@playwright/test';
import {FINISHED, type Gateway, mockGateway} from './gateway.js';

const OUT = process.env['CAPTURE_DIR'];

interface Screen {
  name: string;
  through?: number;
  act?: (page: Page, gateway: Gateway) => Promise<void>;
}

const SCREENS: Screen[] = [{name: 'live'}, {name: 'finished', through: FINISHED}];

test.describe('screens', () => {
  test.skip(OUT === undefined, 'Set CAPTURE_DIR to write the frames');
  for (const screen of SCREENS) {
    test(screen.name, async ({page}) => {
      const gateway = await mockGateway(
        page,
        screen.through === undefined ? {} : {through: screen.through},
      );
      await page.clock.setFixedTime(new Date('2026-09-25T14:02:00Z'));
      await page.setViewportSize({width: 1440, height: 900});
      await page.goto('/?token=e2e');
      await page.getByText('llm-serve').first().waitFor();
      await screen.act?.(page, gateway);
      for (const width of [1440, 1024]) {
        for (const theme of ['dark', 'light'] as const) {
          await page.setViewportSize({width, height: 900});
          await page.emulateMedia({colorScheme: theme, reducedMotion: 'reduce'});
          await page.screenshot({path: join(OUT ?? '', `${screen.name}-${width}-${theme}.png`)});
        }
      }
    });
  }
});
