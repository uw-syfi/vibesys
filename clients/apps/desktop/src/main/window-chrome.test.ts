import {describe, expect, it} from '@effect/vitest';
import fc from 'fast-check';
import {chromeCss, windowChrome, windowChromeOptions} from './window-chrome.js';

const platforms = fc.constantFrom('darwin', 'win32', 'linux');

describe('the window chrome', () => {
  it('has no native title bar on any system', () => {
    fc.assert(
      fc.property(platforms, platform => {
        expect(['hidden', 'hiddenInset']).toContain(windowChromeOptions(platform).titleBarStyle);
      }),
    );
  });

  it('insets the traffic lights on macOS and overlays the controls elsewhere', () => {
    expect(windowChromeOptions('darwin')).toMatchObject({
      titleBarStyle: 'hiddenInset',
      trafficLightPosition: {x: 10, y: 13},
    });
    expect(windowChromeOptions('linux').titleBarOverlay).toBeDefined();
  });

  it('keeps the page clear of the controls, and of nothing in full screen', () => {
    fc.assert(
      fc.property(platforms, platform => {
        const shown = windowChrome(platform, false);
        expect(shown.startWidth + shown.endHeight).toBeGreaterThan(0);
        expect(windowChrome(platform, true)).toEqual({startWidth: 0, startHeight: 0, endHeight: 0});
      }),
    );
    expect(chromeCss(windowChrome('darwin', true))).toContain('--chrome-start-width:0px');
  });

  it('is applied by the one place that opens a window', async () => {
    const {readFile} = await import('node:fs/promises');
    const source = await readFile(new URL('./index.ts', import.meta.url), 'utf8');
    expect(source.match(/new BrowserWindow\(/g)).toHaveLength(1);
    expect(source).toContain('...windowChromeOptions(process.platform)');
    expect(source).toContain('...MINIMUM_WINDOW');
  });
});
