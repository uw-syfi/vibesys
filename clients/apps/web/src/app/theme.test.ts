import {expect, test} from 'bun:test';
import {
  applyWebTheme,
  BrowserWebThemePreferenceStore,
  createWebThemeController,
  parseWebThemeName,
  selectedWebTheme,
  WEB_THEME_NAMES,
  WEB_THEME_TOKENS,
  type WebThemeName,
  type WebThemePreferenceStore,
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

class FakeThemePreferences implements WebThemePreferenceStore {
  value: string | null;
  readonly saved: WebThemeName[] = [];
  removals = 0;

  constructor(value: string | null = null) {
    this.value = value;
  }

  read(): string | null {
    return this.value;
  }

  save(theme: WebThemeName): void {
    this.value = theme;
    this.saved.push(theme);
  }

  remove(): void {
    this.value = null;
    this.removals += 1;
  }
}

class FakeThemeStorage {
  readonly values = new Map<string, string>();
  getItem(key: string): string | null {
    return this.values.get(key) ?? null;
  }
  setItem(key: string, value: string): void {
    this.values.set(key, value);
  }
  removeItem(key: string): void {
    this.values.delete(key);
  }
}

/** Let settled machine operations report without clocks or scheduler-dependent waits. */
async function flush(): Promise<void> {
  for (let turn = 0; turn < 10; turn += 1) await Promise.resolve();
}

test('maps every server-advertised TUI theme name to one Web token set', () => {
  expect(new Set(WEB_THEME_NAMES).size).toBe(WEB_THEME_NAMES.length);
  for (const name of WEB_THEME_NAMES) expect(webThemeName(name)).toBe(name);
});

test('uses the readable default for an absent or unknown server theme', () => {
  const target = new FakeThemeTarget();

  applyWebTheme(target, 'not-a-theme');

  expect(target.dataset['theme']).toBe('dark');
  expect(target.values.get('--page-background')).toBe(WEB_THEME_TOKENS.dark.pageBackground);
});

test('applies and persists an override immediately, restores it, then resets to the latest host default', () => {
  const preferences = new FakeThemePreferences();
  const firstTarget = new FakeThemeTarget();
  const first = createWebThemeController(firstTarget, preferences);

  first.dispatch({type: 'select', theme: 'catppuccin-mocha'});
  expect(firstTarget.dataset['theme']).toBe('catppuccin-mocha');
  expect(preferences.value).toBe('catppuccin-mocha');
  expect(preferences.saved).toEqual(['catppuccin-mocha']);

  const reloadTarget = new FakeThemeTarget();
  const reloaded = createWebThemeController(reloadTarget, preferences);
  expect(reloadTarget.dataset['theme']).toBe('catppuccin-mocha');
  expect(selectedWebTheme(reloaded.getState())).toBe('catppuccin-mocha');

  reloaded.dispatch({type: 'host-default', theme: 'solarized-light'});
  expect(reloadTarget.dataset['theme']).toBe('catppuccin-mocha');
  reloaded.dispatch({type: 'reset'});
  expect(reloadTarget.dataset['theme']).toBe('solarized-light');
  expect(preferences.value).toBeNull();
  expect(reloaded.getState()).toEqual({hostDefault: 'solarized-light', override: null});
});

test('rejects and removes a stale stored value instead of mapping it to another override', () => {
  const preferences = new FakeThemePreferences('retired-theme');
  const target = new FakeThemeTarget();

  const theme = createWebThemeController(target, preferences);

  expect(parseWebThemeName('retired-theme')).toBeNull();
  expect(theme.getState()).toEqual({hostDefault: 'dark', override: null});
  expect(target.dataset['theme']).toBe('dark');
  expect(preferences.value).toBeNull();
  expect(preferences.removals).toBe(1);
});

test('the browser preference implementation stores only the selected theme value', () => {
  const storage = new FakeThemeStorage();
  const preferences = new BrowserWebThemePreferenceStore(() => storage);

  preferences.save('high-contrast-dark');

  expect([...storage.values.entries()]).toEqual([['vibesys.web.theme', 'high-contrast-dark']]);
  expect(preferences.read()).toBe('high-contrast-dark');
  preferences.remove();
  expect(storage.values.size).toBe(0);
});

test('queries the host default only after live startup accepts a snapshot', async () => {
  const target = new FakeThemeTarget();
  const theme = createWebThemeController(target, new FakeThemePreferences());
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

  theme.resolveSessionDefault(session);

  expect(target.dataset['theme']).toBe('dark');
  expect(calls).toEqual(['start']);
  finishStartup();
  await flush();
  expect(calls).toEqual(['start', 'tui-defaults']);
  expect(target.dataset['theme']).toBe('light');
});

test('does not open the control channel for a theme after cold stream startup fails', async () => {
  const target = new FakeThemeTarget();
  const theme = createWebThemeController(target, new FakeThemePreferences());
  let defaultsQueries = 0;
  theme.resolveSessionDefault({
    start: () => Promise.resolve(),
    getState: () => ({hasSnapshot: false}),
    tuiDefaults: () => {
      defaultsQueries += 1;
      return Promise.resolve({theme: 'light'});
    },
  });
  await flush();

  expect(defaultsQueries).toBe(0);
  expect(target.dataset['theme']).toBe('dark');
});

test('a failed current host-default request settles on the documented default', async () => {
  const target = new FakeThemeTarget();
  const theme = createWebThemeController(target, new FakeThemePreferences());
  theme.dispatch({type: 'host-default', theme: 'light'});

  theme.resolveSessionDefault({
    start: () => Promise.resolve(),
    getState: () => ({hasSnapshot: true}),
    tuiDefaults: () => Promise.reject(new Error('control channel unavailable')),
  });

  expect(target.dataset['theme']).toBe('dark');
  await flush();
  expect(theme.getState().hostDefault).toBe('dark');
  expect(target.dataset['theme']).toBe('dark');
});

test('a late default from a previous run cannot replace the current host default', async () => {
  const target = new FakeThemeTarget();
  const theme = createWebThemeController(target, new FakeThemePreferences());
  let finishOldDefault: (value: {theme: 'catppuccin-latte'}) => void = () => undefined;
  const oldDefault = new Promise<{theme: 'catppuccin-latte'}>(resolve => {
    finishOldDefault = resolve;
  });
  let oldQueryStarted: () => void = () => undefined;
  const oldQuery = new Promise<void>(resolve => {
    oldQueryStarted = resolve;
  });
  theme.resolveSessionDefault({
    start: () => Promise.resolve(),
    getState: () => ({hasSnapshot: true}),
    tuiDefaults: () => {
      oldQueryStarted();
      return oldDefault;
    },
  });
  await oldQuery;

  theme.resolveSessionDefault({
    start: () => Promise.resolve(),
    getState: () => ({hasSnapshot: true}),
    tuiDefaults: () => Promise.resolve({theme: 'light'}),
  });
  await flush();
  finishOldDefault({theme: 'catppuccin-latte'});
  await flush();

  expect(theme.getState().hostDefault).toBe('light');
  expect(target.dataset['theme']).toBe('light');
});

test('a superseded host never opens its defaults channel after pending startup finishes', async () => {
  const target = new FakeThemeTarget();
  const theme = createWebThemeController(target, new FakeThemePreferences());
  let finishOldStartup: () => void = () => undefined;
  const oldStartup = new Promise<void>(resolve => {
    finishOldStartup = resolve;
  });
  let oldQueries = 0;
  let currentQueryStarted: () => void = () => undefined;
  const currentQuery = new Promise<void>(resolve => {
    currentQueryStarted = resolve;
  });
  let finishCurrentDefault: (value: {theme: 'high-contrast-light'}) => void = () => undefined;
  const currentDefault = new Promise<{theme: 'high-contrast-light'}>(resolve => {
    finishCurrentDefault = resolve;
  });
  theme.resolveSessionDefault({
    start: () => oldStartup,
    getState: () => ({hasSnapshot: true}),
    tuiDefaults: () => {
      oldQueries += 1;
      return Promise.resolve({theme: 'catppuccin-latte'});
    },
  });

  theme.resolveSessionDefault({
    start: () => Promise.resolve(),
    getState: () => ({hasSnapshot: true}),
    tuiDefaults: () => {
      currentQueryStarted();
      return currentDefault;
    },
  });
  await currentQuery;
  const currentApplied = new Promise<void>(resolve => {
    const unsubscribe = theme.subscribe(() => {
      if (theme.getState().hostDefault !== 'high-contrast-light') return;
      unsubscribe();
      resolve();
    });
  });
  finishCurrentDefault({theme: 'high-contrast-light'});
  await currentApplied;
  finishOldStartup();
  await oldStartup;

  expect(oldQueries).toBe(0);
  expect(theme.getState().hostDefault).toBe('high-contrast-light');
  expect(target.dataset['theme']).toBe('high-contrast-light');
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
