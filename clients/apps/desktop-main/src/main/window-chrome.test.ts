import {describe, expect, test} from 'bun:test';
import {chromeInset, TITLEBAR_HEIGHT, windowChrome} from './window-chrome.js';

describe('windowChrome', () => {
  test('every platform paints the same page background so nothing flashes before load', () => {
    const colors = ['darwin', 'win32', 'linux', 'freebsd'].map(
      platform => windowChrome(platform).backgroundColor,
    );
    expect(new Set(colors).size).toBe(1);
    expect(colors[0]).toMatch(/^#[0-9a-f]{6}$/);
  });

  test('macOS keeps the traffic lights vertically centered in the strip', () => {
    const chrome = windowChrome('darwin');
    expect(chrome.titleBarStyle).toBe('hiddenInset');
    const y = chrome.trafficLightPosition?.y ?? Number.NaN;
    expect(y * 2 + 12).toBe(TITLEBAR_HEIGHT);
  });

  test('other platforms overlay window controls on the page background at strip height', () => {
    for (const platform of ['win32', 'linux']) {
      const chrome = windowChrome(platform);
      expect(chrome.titleBarStyle).toBe('hidden');
      expect(chrome.titleBarOverlay?.color).toBe(chrome.backgroundColor);
      expect(chrome.titleBarOverlay?.height).toBe(TITLEBAR_HEIGHT);
      expect(chrome.trafficLightPosition).toBeUndefined();
    }
  });
});

describe('chromeInset', () => {
  test('on macOS covers the traffic lights with a margin past the last one, over the strip', () => {
    const inset = chromeInset('darwin');
    const lights = windowChrome('darwin').trafficLightPosition;
    const lastLightEnd = (lights?.x ?? 0) + 3 * 12 + 2 * 8;
    expect(inset.width).toBeGreaterThan(lastLightEnd);
    expect(inset.height).toBe(TITLEBAR_HEIGHT);
    expect((lights?.y ?? 0) + 12).toBeLessThanOrEqual(inset.height);
  });

  test('is empty where the controls are drawn at the top right instead', () => {
    for (const platform of ['win32', 'linux', 'freebsd']) {
      expect(chromeInset(platform)).toEqual({width: 0, height: 0});
    }
  });
});
