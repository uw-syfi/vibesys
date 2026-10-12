import {expect, test} from 'bun:test';
import {
  applySessionTheme,
  applyWebTheme,
  WEB_THEME_NAMES,
  WEB_THEME_TOKENS,
  type WebThemeTokens,
  webThemeName,
} from './theme.js';

class FakeThemeTarget {
  readonly dataset: DOMStringMap = {};
  readonly values = new Map<string, string>();
  readonly style = {
    setProperty: (name: string, value: string): void => {
      this.values.set(name, value);
    },
  };
}

test('maps every server-advertised TUI theme name to one Web token set', () => {
  expect(WEB_THEME_NAMES).toHaveLength(8);
  for (const name of WEB_THEME_NAMES) expect(webThemeName(name)).toBe(name);
});

test('uses the readable default for an absent or unknown server theme', () => {
  const target = new FakeThemeTarget();

  applyWebTheme(target, 'not-a-theme');

  expect(target.dataset['theme']).toBe('dark');
  expect(target.values.get('--page-background')).toBe(WEB_THEME_TOKENS.dark.pageBackground);
});

test('queries the server theme only after live startup accepts a snapshot', async () => {
  const target = new FakeThemeTarget();
  let finishStartup: () => void = () => undefined;
  const startup = new Promise<void>(resolve => {
    finishStartup = resolve;
  });
  const calls: string[] = [];
  const session = {
    hasSnapshot: true,
    start: () => {
      calls.push('start');
      return startup;
    },
    getState() {
      return {hasSnapshot: this.hasSnapshot};
    },
    tuiDefaults: () => {
      calls.push('tui-defaults');
      return Promise.resolve({theme: 'light' as const});
    },
  };

  const applying = applySessionTheme(target, session);

  expect(target.dataset['theme']).toBe('dark');
  expect(calls).toEqual(['start']);
  finishStartup();
  await applying;
  expect(calls).toEqual(['start', 'tui-defaults']);
  expect(target.dataset['theme']).toBe('light');
});

test('does not open the control channel for a theme after cold stream startup fails', async () => {
  const target = new FakeThemeTarget();
  let defaultsQueries = 0;
  await applySessionTheme(target, {
    start: () => Promise.resolve(),
    getState: () => ({hasSnapshot: false}),
    tuiDefaults: () => {
      defaultsQueries += 1;
      return Promise.resolve({theme: 'light'});
    },
  });

  expect(defaultsQueries).toBe(0);
  expect(target.dataset['theme']).toBe('dark');
});

test('applies a complete distinct palette for every advertised theme', () => {
  for (const name of WEB_THEME_NAMES) {
    const target = new FakeThemeTarget();
    applyWebTheme(target, name);
    expect(target.dataset['theme']).toBe(name);
    expect(target.values.size).toBe(Object.keys(WEB_THEME_TOKENS[name]).length);
    expect(target.values.get('--surface')).toBe(WEB_THEME_TOKENS[name].surface);
    expect(target.values.get('--on-accent')).toBe(WEB_THEME_TOKENS[name].onAccent);
  }
});

test('keeps every text token readable on the surface where the viewer uses it', () => {
  for (const name of WEB_THEME_NAMES) {
    const palette = WEB_THEME_TOKENS[name];
    const combinations: ReadonlyArray<readonly [keyof WebThemeTokens, keyof WebThemeTokens]> = [
      ['text', 'surface'],
      ['subtle', 'surface'],
      ['success', 'surface'],
      ['failure', 'surface'],
      ['accent', 'pageBackground'],
      ['warning', 'warningSurface'],
      ['onAccent', 'accent'],
      ['onFailure', 'failure'],
    ];
    for (const [foreground, background] of combinations) {
      expect(
        contrast(palette[foreground], palette[background]),
        `${name} ${foreground}/${background}`,
      ).toBeGreaterThanOrEqual(4.5);
    }
  }
});

test('keeps form field boundaries distinguishable from their actual fill', () => {
  for (const name of WEB_THEME_NAMES) {
    const palette = WEB_THEME_TOKENS[name];
    expect(
      contrast(palette.border, palette.fieldBackground),
      `${name} border/fieldBackground`,
    ).toBeGreaterThanOrEqual(3);
  }
});

function contrast(foreground: string, background: string): number {
  const lighter = Math.max(luminance(foreground), luminance(background));
  const darker = Math.min(luminance(foreground), luminance(background));
  return (lighter + 0.05) / (darker + 0.05);
}

function luminance(hex: string): number {
  const channels = [1, 3, 5].map(
    offset => Number.parseInt(hex.slice(offset, offset + 2), 16) / 255,
  );
  const [red = 0, green = 0, blue = 0] = channels.map(channel =>
    channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4,
  );
  return red * 0.2126 + green * 0.7152 + blue * 0.0722;
}
