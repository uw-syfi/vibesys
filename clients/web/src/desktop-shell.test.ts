import {describe, expect, test} from 'bun:test';
import {desktopShellAttributes, markDesktopShell} from './desktop-shell.js';

describe('desktopShellAttributes', () => {
  test('describes every supported platform', () => {
    for (const platform of ['darwin', 'win32', 'linux'] as const) {
      expect(desktopShellAttributes({platform})).toEqual({
        'data-shell': 'desktop',
        'data-platform': platform,
      });
    }
  });

  test('leaves a plain browser and malformed bridges unmarked', () => {
    const rejected: unknown[] = [
      undefined,
      null,
      'darwin',
      42,
      {},
      {platform: 'freebsd'},
      {platform: 7},
      {platform: null},
    ];
    for (const bridge of rejected) expect(desktopShellAttributes(bridge)).toBeNull();
  });
});

describe('markDesktopShell', () => {
  test('sets the attributes on the root only inside the desktop shell', () => {
    const attributes = new Map<string, string>();
    const root = {setAttribute: (name: string, value: string) => attributes.set(name, value)};
    markDesktopShell(root as unknown as HTMLElement, undefined);
    expect(attributes.size).toBe(0);
    markDesktopShell(root as unknown as HTMLElement, {platform: 'darwin'});
    expect(Object.fromEntries(attributes)).toEqual({
      'data-shell': 'desktop',
      'data-platform': 'darwin',
    });
  });
});
