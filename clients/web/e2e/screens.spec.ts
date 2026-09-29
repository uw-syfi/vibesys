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

const runs = (page: Page) => page.getByRole('navigation', {name: 'Runs'});

const SCREENS: Screen[] = [
  {name: 'live'},
  {name: 'finished', through: FINISHED},
  {
    name: 'round3',
    act: async page => {
      await runs(page)
        .getByRole('button', {name: /^Round 3,/})
        .click();
      await page.getByRole('button', {name: /cargo test --release sampler.*exit 1/}).click();
    },
  },
  {
    name: 'steer',
    act: async page => {
      const box = page.getByRole('textbox', {name: 'Steer the next agent call'});
      await box.fill('Measure lock hold time before replacing the queue.');
      await box.press('Enter');
      await page.getByText('Queued for the next agent call').waitFor();
    },
  },
  {
    name: 'pausing',
    act: async page => {
      await page.locator('.titlebar').getByRole('button', {name: 'Pause'}).click();
      await page.getByText('Pausing after the current call…').waitFor();
    },
  },
  {
    name: 'paused',
    act: async (page, gateway) => {
      await page.locator('.titlebar').getByRole('button', {name: 'Pause'}).click();
      gateway.setStatus('paused');
      await page.getByText('Paused in round 6').waitFor();
    },
  },
  {
    name: 'stop',
    act: async page => {
      await page.getByRole('button', {name: 'More'}).click();
      await page.getByRole('menuitem', {name: 'Stop run…'}).click();
      await page.getByRole('alertdialog').waitFor();
    },
  },
  {name: 'pane', act: async page => page.getByRole('button', {name: 'Toggle side pane'}).click()},
  {name: 'collapsed', act: async page => page.getByRole('button', {name: 'Hide sidebar'}).click()},
  {
    name: 'ask',
    act: async page => {
      await page.getByRole('button', {name: 'Toggle side pane'}).click();
      await page.getByRole('tab', {name: 'Ask'}).click();
    },
  },
];

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
      await page.locator('.titlebar').waitFor();
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
