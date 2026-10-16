import {describe, expect, test} from 'bun:test';
import {join} from 'node:path';
import {assetPath, isAppPage, isSameAppPage, SHELL_URL} from './app-assets.js';

const ROOT = '/opt/vibesys/ui';

describe('assetPath', () => {
  test('serves the entry page and its assets from the root', () => {
    expect(assetPath(SHELL_URL, ROOT)).toBe(join(ROOT, 'shell.html'));
    expect(assetPath('app://vibesys/assets/shell-B1.js', ROOT)).toBe(
      join(ROOT, 'assets/shell-B1.js'),
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
      'http://vibesys/shell.html',
      'app://other/shell.html',
      'file:///opt/vibesys/ui/shell.html',
      'app://vibesys/run.sh',
      'not a url',
    ]) {
      expect({url, path: assetPath(url, ROOT)}).toEqual({url, path: null});
    }
  });
});

describe('isAppPage', () => {
  test('accepts only the app origin', () => {
    expect(isAppPage(SHELL_URL)).toBe(true);
    for (const url of ['http://127.0.0.1:8765/', 'app://evil/', 'https://vibesys/', 'nope']) {
      expect(isAppPage(url)).toBe(false);
    }
  });
});

describe('isSameAppPage', () => {
  test('admits only the exact resource, independent of query or fragment', () => {
    for (const url of [
      SHELL_URL,
      `${SHELL_URL}?source=launch`,
      `${SHELL_URL}#run`,
      `${SHELL_URL}?source=launch#run`,
    ]) {
      expect({url, same: isSameAppPage(url, SHELL_URL)}).toEqual({url, same: true});
    }
    for (const url of [
      null,
      'app://vibesys/welcome.html',
      'app://vibesys/shell.html-preview.html',
      'app://vibesys/shell.html/child',
      'app://other/shell.html',
      'app://user@vibesys/shell.html',
      'app://vibesys:443/shell.html',
      'https://vibesys/shell.html',
      'not a url',
    ]) {
      expect({url, same: isSameAppPage(url, SHELL_URL)}).toEqual({url, same: false});
    }
  });

  test('refuses an expected URL outside the app origin', () => {
    expect(isSameAppPage('https://vibesys/shell.html', 'https://vibesys/shell.html')).toBe(false);
  });
});
