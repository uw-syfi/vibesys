import {describe, expect, test} from 'bun:test';
import fc from 'fast-check';
import {chromeInset, OVERLAY_HEIGHT, windowChrome} from './window-chrome.js';

const LIGHT = 12;
const LIGHTS_WIDTH = 3 * LIGHT + 2 * 8;

const named = fc.constantFrom('darwin', 'win32', 'linux', 'freebsd');
/** Any `process.platform` value: a known one, or whatever a future Electron reports. */
const platform = fc.oneof(named, fc.string());
const elsewhere = platform.filter(each => each !== 'darwin');

describe('windowChrome', () => {
  test('every platform paints the same page background so nothing flashes before load', () => {
    const colors = ['darwin', 'win32', 'linux', 'freebsd'].map(
      each => windowChrome(each).backgroundColor,
    );
    expect(new Set(colors).size).toBe(1);
    expect(colors[0]).toMatch(/^#[0-9a-f]{6}$/);
  });

  test('macOS hides the title bar and places the lights at the margin the inset reserves', () => {
    const chrome = windowChrome('darwin');
    expect(chrome.titleBarStyle).toBe('hiddenInset');
    expect(chrome.trafficLightPosition).toBeDefined();
    expect(chrome.titleBarOverlay).toBeUndefined();
  });

  test('other platforms overlay window controls on the page background at the strip height', () => {
    fc.assert(
      fc.property(elsewhere, each => {
        const chrome = windowChrome(each);
        expect(chrome.titleBarStyle).toBe('hidden');
        expect(chrome.titleBarOverlay?.color).toBe(chrome.backgroundColor);
        expect(chrome.titleBarOverlay?.height).toBe(OVERLAY_HEIGHT);
        expect(chrome.trafficLightPosition).toBeUndefined();
      }),
    );
  });
});

describe('chromeInset', () => {
  test('on macOS the lights sit inside the reserved corner, with the same margin on both sides and centered in its height', () => {
    const inset = chromeInset('darwin');
    const lights = windowChrome('darwin').trafficLightPosition ?? {x: Number.NaN, y: Number.NaN};
    expect(lights.x).toBeGreaterThan(0);
    expect(inset.startWidth - (lights.x + LIGHTS_WIDTH)).toBe(lights.x);
    expect(lights.y).toBeGreaterThan(0);
    expect(inset.startHeight - (lights.y + LIGHT)).toBe(lights.y);
    expect(inset.endHeight).toBe(0);
  });

  test('elsewhere the controls cover a strip across the top and no corner, as high as the overlay', () => {
    fc.assert(
      fc.property(elsewhere, each => {
        const overlay = windowChrome(each).titleBarOverlay?.height;
        expect(chromeInset(each)).toEqual({
          startWidth: 0,
          startHeight: 0,
          endHeight: overlay ?? -1,
        });
      }),
    );
  });

  test('a platform never has its controls at both ends, and a corner has both a width and a height', () => {
    fc.assert(
      fc.property(platform, each => {
        const inset = chromeInset(each);
        expect(inset.startWidth > 0 === inset.startHeight > 0).toBe(true);
        expect(inset.startWidth > 0 && inset.endHeight > 0).toBe(false);
        expect(inset.startWidth > 0 || inset.endHeight > 0).toBe(true);
      }),
    );
  });

  test('full screen hides the controls on every platform, so nothing is reserved', () => {
    fc.assert(
      fc.property(platform, each => {
        expect(chromeInset(each, true)).toEqual({startWidth: 0, startHeight: 0, endHeight: 0});
      }),
    );
  });
});
