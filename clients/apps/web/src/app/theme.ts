/** Web token names deliberately match the server's TUI theme names. The browser owns only
 * its token set, not the TUI renderer or its implementation details. */

import {
  createHostThemeDefaultStore,
  DEFAULT_WEB_THEME,
  WEB_THEME_NAMES,
  WEB_THEME_TOKENS,
  type WebThemeName,
  type WebThemeSession,
  type WebThemeTokens,
} from '@vibesys/feature-web-theme/state';
import {createStore, type Reducer, type Store} from '@vibesys/store';
import type {AbortSignalLike} from '@vibesys/transport';

export {
  WEB_THEME_NAMES,
  WEB_THEME_TOKENS,
  type WebThemeName,
  type WebThemeTokens,
} from '@vibesys/feature-web-theme/state';

const CSS_TOKEN_NAMES: Readonly<Record<keyof WebThemeTokens, string>> = {
  pageBackground: '--page-background',
  surface: '--surface',
  border: '--border',
  text: '--text',
  subtle: '--subtle',
  accent: '--accent',
  onAccent: '--on-accent',
  success: '--success',
  failure: '--failure',
  onFailure: '--on-failure',
  warning: '--warning',
  warningSurface: '--warning-surface',
  fieldBackground: '--field-background',
};

export interface WebThemeTarget {
  readonly dataset: {[key: string]: string | undefined};
  readonly style: Pick<CSSStyleDeclaration, 'setProperty'>;
}

/** The app shell's deliberately narrow durable store: it can persist a theme name and nothing else. */
export interface WebThemePreferenceStore {
  read(): string | null;
  save(theme: WebThemeName): void;
  remove(): void;
}

export interface WebThemePreferenceState {
  readonly hostDefault: WebThemeName;
  readonly override: WebThemeName | null;
}

type WebThemePreferenceAction =
  | {readonly type: 'host-default'; readonly theme: WebThemeName}
  | {readonly type: 'select'; readonly theme: WebThemeName}
  | {readonly type: 'reset'};

type WebThemePreferenceRequest =
  | {readonly type: 'apply'; readonly theme: WebThemeName}
  | {readonly type: 'save'; readonly theme: WebThemeName}
  | {readonly type: 'remove'};

export interface WebThemeController
  extends Store<WebThemePreferenceState, WebThemePreferenceAction> {
  /** Resolve one host's default; a newer call supersedes every older unfinished call. */
  resolveSessionDefault(session: WebThemeSession): void;
}

/** The only Web preference key. Its value is one generated theme name, never run or capability data. */
const WEB_THEME_PREFERENCE_KEY = 'vibesys.web.theme';

type ThemeStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>;

/** Browser implementation of the app-owned theme preference. Storage denial is non-fatal. */
export class BrowserWebThemePreferenceStore implements WebThemePreferenceStore {
  readonly #storage: () => ThemeStorage;

  constructor(storage: () => ThemeStorage) {
    this.#storage = storage;
  }

  read(): string | null {
    try {
      return this.#storage().getItem(WEB_THEME_PREFERENCE_KEY);
    } catch {
      return null;
    }
  }

  save(theme: WebThemeName): void {
    try {
      this.#storage().setItem(WEB_THEME_PREFERENCE_KEY, theme);
    } catch {
      // The selected theme still applies for this document when storage is unavailable.
    }
  }

  remove(): void {
    try {
      this.#storage().removeItem(WEB_THEME_PREFERENCE_KEY);
    } catch {
      // Reset still applies the host default for this document when storage is unavailable.
    }
  }
}

/** Strict boundary parser. Unlike `webThemeName`, invalid persisted values have no fallback value. */
export function parseWebThemeName(value: string | null | undefined): WebThemeName | null {
  return WEB_THEME_NAMES.includes(value as WebThemeName) ? (value as WebThemeName) : null;
}

export function webThemeName(value: string | null | undefined): WebThemeName {
  return parseWebThemeName(value) ?? DEFAULT_WEB_THEME;
}

/** Apply one complete validated palette to the document. */
export function applyWebTheme(target: WebThemeTarget, value: string | null | undefined): void {
  const name = webThemeName(value);
  target.dataset['theme'] = name;
  const tokens = WEB_THEME_TOKENS[name];
  for (const key of Object.keys(CSS_TOKEN_NAMES) as Array<keyof WebThemeTokens>) {
    target.style.setProperty(CSS_TOKEN_NAMES[key], tokens[key]);
  }
}

export function selectedWebTheme(state: WebThemePreferenceState): WebThemeName {
  return state.override ?? state.hostDefault;
}

const reduceWebThemePreference: Reducer<
  WebThemePreferenceState,
  WebThemePreferenceAction,
  WebThemePreferenceRequest
> = (state, action) => {
  switch (action.type) {
    case 'host-default': {
      const next = {...state, hostDefault: action.theme};
      return {state: next, requests: [{type: 'apply', theme: selectedWebTheme(next)}]};
    }
    case 'select': {
      const next = {...state, override: action.theme};
      return {
        state: next,
        requests: [
          {type: 'apply', theme: action.theme},
          {type: 'save', theme: action.theme},
        ],
      };
    }
    case 'reset': {
      const next = {...state, override: null};
      return {
        state: next,
        requests: [{type: 'apply', theme: next.hostDefault}, {type: 'remove'}],
      };
    }
  }
};

/** The app shell's host-session operation; XState owns its invocation and cancellation. */
async function resolveHostTheme(
  session: WebThemeSession,
  signal: AbortSignalLike,
): Promise<WebThemeName> {
  await session.start();
  if (signal.aborted) throw signal.reason;
  if (!session.getState().hasSnapshot) return DEFAULT_WEB_THEME;
  if (signal.aborted) throw signal.reason;
  return (await session.tuiDefaults())?.theme ?? DEFAULT_WEB_THEME;
}

/**
 * Own the Web theme for one document.
 *
 * The pure reducer decides the effective theme and persistence intent. This thin shell validates
 * the stored boundary once and performs only theme-scoped I/O. The host-default machine owns
 * startup, cancellation, failure, and late replies when the page moves between live sessions.
 */
export function createWebThemeController(
  target: WebThemeTarget,
  preferences: WebThemePreferenceStore,
): WebThemeController {
  const stored = preferences.read();
  const override = parseWebThemeName(stored);
  const initial: WebThemePreferenceState = {hostDefault: DEFAULT_WEB_THEME, override};
  if (stored !== null && override === null) preferences.remove();
  applyWebTheme(target, selectedWebTheme(initial));

  const store = createStore(reduceWebThemePreference, initial, request => {
    switch (request.type) {
      case 'apply':
        applyWebTheme(target, request.theme);
        return;
      case 'save':
        preferences.save(request.theme);
        return;
      case 'remove':
        preferences.remove();
        return;
    }
  });
  const hostDefault = createHostThemeDefaultStore(resolveHostTheme);
  hostDefault.subscribe(() => {
    store.dispatch({type: 'host-default', theme: hostDefault.getState().theme});
  });
  return {
    ...store,
    resolveSessionDefault(session): void {
      hostDefault.dispatch({type: 'resolve', session});
    },
  };
}
