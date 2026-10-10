import {describe, expect, test} from 'bun:test';
import {PAGE_BACKGROUND, TITLEBAR_HEIGHT, windowChrome} from './window-chrome.js';

describe('windowChrome', () => {
  test('every platform paints the page background so nothing flashes before load', () => {
    for (const platform of ['darwin', 'win32', 'linux', 'freebsd']) {
      expect(windowChrome(platform).backgroundColor).toBe(PAGE_BACKGROUND);
    }
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
      expect(chrome.titleBarOverlay?.color).toBe(PAGE_BACKGROUND);
      expect(chrome.titleBarOverlay?.height).toBe(TITLEBAR_HEIGHT);
      expect(chrome.trafficLightPosition).toBeUndefined();
    }
  });
});
