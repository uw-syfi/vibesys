import {describe, expect, test} from 'bun:test';
import {join} from 'node:path';
import {
  APP_ENTRY_URL,
  assetPath,
  isAppPage,
  isSameAppPage,
  SHELL_URL,
  surfaceUrl,
  WELCOME_URL,
} from './app-assets.js';

const ROOT = '/opt/vibesys/ui';

describe('assetPath', () => {
  test('serves the entry page and its assets from the root', () => {
    expect(assetPath(APP_ENTRY_URL, ROOT)).toBe(join(ROOT, 'desktop.html'));
    expect(assetPath('app://vibesys/assets/desktop-B1.js', ROOT)).toBe(
      join(ROOT, 'assets/desktop-B1.js'),
    );
    expect(assetPath('app://vibesys/assets/a%20b.css?x=1#y', ROOT)).toBe(
      join(ROOT, 'assets/a b.css'),
    );
  });

  test('never resolves outside the root, whatever the encoding', () => {
    const hostile = [
      '..',
      '%2e%2e',
      '.%2e',
      '..%2f..',
      '..%5c..',
      '%2e%2e%2f%2e%2e',
      '%00',
      '.vite',
    ];
    for (const first of hostile) {
      for (const second of hostile) {
        const url = `app://vibesys/assets/${first}/${second}/secret.js`;
        const path = assetPath(url, ROOT);
        expect({url, inside: path === null || path.startsWith(`${ROOT}/`)}).toEqual({
          url,
          inside: true,
        });
      }
    }
    for (const url of [
      'app://vibesys/assets/..%2f..%2fsecret.js',
      'app://vibesys/assets/..%5csecret.js',
      'app://vibesys/assets/%00.js',
      'app://vibesys/%E0%A4%A.js',
      'app://vibesys/.vite/manifest.json',
      'app://vibesys/',
    ]) {
      expect({url, path: assetPath(url, ROOT)}).toEqual({url, path: null});
    }
  });

  test('serves only the app origin and known file types', () => {
    for (const url of [
      'http://vibesys/desktop.html',
      'app://other/desktop.html',
      'file:///opt/vibesys/ui/desktop.html',
      'app://vibesys/run.sh',
      'not a url',
    ]) {
      expect({url, path: assetPath(url, ROOT)}).toEqual({url, path: null});
    }
  });
});

describe('isAppPage', () => {
  test('accepts only the app origin', () => {
    expect(isAppPage(APP_ENTRY_URL)).toBe(true);
    for (const url of ['http://127.0.0.1:8765/', 'app://evil/', 'https://vibesys/', 'nope']) {
      expect(isAppPage(url)).toBe(false);
    }
  });
});

describe('isSameAppPage', () => {
  test('admits only the exact resource, independent of query or fragment', () => {
    for (const url of [
      APP_ENTRY_URL,
      `${APP_ENTRY_URL}?source=launch`,
      `${APP_ENTRY_URL}#run`,
      `${APP_ENTRY_URL}?source=launch#run`,
    ]) {
      expect({url, same: isSameAppPage(url, APP_ENTRY_URL)}).toEqual({url, same: true});
    }
    for (const url of [
      null,
      'app://vibesys/welcome.html',
      'app://vibesys/desktop.html-preview.html',
      'app://vibesys/desktop.html/child',
      'app://other/desktop.html',
      'app://user@vibesys/desktop.html',
      'app://vibesys:443/desktop.html',
      'https://vibesys/desktop.html',
      'not a url',
    ]) {
      expect({url, same: isSameAppPage(url, APP_ENTRY_URL)}).toEqual({url, same: false});
    }
  });

  test('refuses an expected URL outside the app origin', () => {
    expect(isSameAppPage('https://vibesys/desktop.html', 'https://vibesys/desktop.html')).toBe(
      false,
    );
  });
});

describe('surfaceUrl', () => {
  test('shows the app shell only for next, the welcome view when unset, and refuses the rest', () => {
    expect(surfaceUrl(undefined)).toBe(WELCOME_URL);
    expect(surfaceUrl('')).toBe(WELCOME_URL);
    expect(surfaceUrl('next')).toBe(SHELL_URL);
    for (const value of ['Next', 'old', '1']) {
      expect(() => surfaceUrl(value)).toThrow('VIBESYS_DESKTOP_SHELL');
    }
  });
});
