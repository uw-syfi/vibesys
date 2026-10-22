import {defineConfig} from '@playwright/test';
import {E2E_HOST, runPort} from '../../tools/e2e/free-port.mjs';
import {e2eTestMatch} from '../../tools/e2e/shard.mjs';

/**
 * The gallery's visual and accessibility suite (rules S11 and S12 of #2827).
 *
 * Screenshots are compared against baselines rendered by Chromium on Linux, the browser and OS the
 * `clients_e2e` CI job uses; the template has no platform suffix, so one set is checked in, and it is
 * written in the same Linux image (`pnpm update:screenshots`). The page draws its text only in the
 * bundled fonts, under reduced motion, at a fixed viewport and scale, with nothing from a clock.
 */
const inCI = !!process.env['CI'];

/**
 * Chromium flags that take the rasterizer's per-run choices out of the picture. Without them two
 * consecutive runs on one machine differ in a few pixels per screen, and a baseline update
 * rewrites a dozen screens nobody changed. The cause is the rasterizer's own choices: LCD
 * (sub-pixel) text is used only where the compositor happens to find an opaque layer, glyph
 * positions and hinting follow the platform's font settings, Skia picks SIMD variants of its
 * blitters at run time, and tiles raster on several threads and partly re-raster. The flags
 * fix one choice each: grayscale text, no hinting, whole-pixel glyphs, one code path, software
 * raster on one thread, whole tiles. `scripts/check-deterministic.sh` fails if two runs differ.
 */
const DETERMINISTIC_RENDERING = [
  '--disable-lcd-text',
  '--font-render-hinting=none',
  '--disable-font-subpixel-positioning',
  '--disable-skia-runtime-opts',
  '--force-color-profile=srgb',
  '--disable-gpu',
  '--num-raster-threads=1',
  '--disable-partial-raster',
];
const port = await runPort('VIBESYS_E2E_UI_GALLERY_PORT');

export default defineConfig({
  testDir: './e2e',
  retries: 0,
  forbidOnly: inCI,
  workers: 1,
  testMatch: e2eTestMatch('ui-gallery'),
  // A hang guard only: the suite takes well under a minute natively, three times that under the
  // x86-64 emulation that writes the baselines on an Arm Mac.
  timeout: 360_000,
  snapshotPathTemplate: '{testDir}/__screenshots__/{arg}{ext}',
  // Exact comparison: the rendering is deterministic (see DETERMINISTIC_RENDERING), so a baseline
  // that differs at all is stale, and the default per-pixel tolerance would let it pass unseen.
  expect: {
    toHaveScreenshot: {
      animations: 'disabled',
      caret: 'hide',
      scale: 'css',
      threshold: 0,
      maxDiffPixels: 0,
    },
  },
  webServer: {
    command: `node e2e/serve-dist.mjs ${port}`,
    port,
    reuseExistingServer: false,
  },
  use: {
    baseURL: `http://${E2E_HOST}:${port}`,
    browserName: 'chromium',
    launchOptions: {args: DETERMINISTIC_RENDERING},
    viewport: {width: 1280, height: 900},
    deviceScaleFactor: 1,
    colorScheme: 'light',
    contextOptions: {reducedMotion: 'reduce'},
    locale: 'en-US',
    timezoneId: 'UTC',
  },
});
