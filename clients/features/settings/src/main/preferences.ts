/**
 * The settings feature in the main process: it answers the views' preference requests through the
 * settings store, keeps the native theme (window chrome, menus, `prefers-color-scheme`) on the
 * appearance's mode, and resolves the theme a page paints in first.
 */
import type {PreferencesView, WelcomeRequest} from '@vibesys/ipc';
import {
  DEFAULT_PREFERENCES,
  type Preferences,
  type SettingsStore,
  withArchived,
} from '@vibesys/settings';
import {type AppearanceMode, effectiveTheme, type ThemeName} from '@vibesys/ui/tokens';

/** The settings requests a view sends, as `@vibesys/ipc` parsed them. */
export type PreferencesRequest = Extract<
  WelcomeRequest,
  {readonly kind: 'preferences' | 'set-appearance' | 'set-notifications' | 'archive'}
>;

/** The part of Electron's `nativeTheme` this feature drives and reads. */
export interface NativeTheme {
  themeSource: AppearanceMode;
  readonly shouldUseDarkColors: boolean;
}

export interface PreferencesMainDeps {
  readonly store: Pick<SettingsStore, 'preferences' | 'updatePreferences' | 'onPreferencesChanged'>;
  readonly nativeTheme: NativeTheme;
  /** Tell the views the preferences changed. */
  readonly push: (preferences: PreferencesView) => void;
}

function view(preferences: Preferences): PreferencesView {
  const {appearance, notifications, archived} = preferences;
  return {appearance, notifications, archived};
}

export class PreferencesMain {
  readonly #deps: PreferencesMainDeps;
  #current: Preferences = DEFAULT_PREFERENCES;

  constructor(deps: PreferencesMainDeps) {
    this.#deps = deps;
    deps.store.onPreferencesChanged(preferences => this.#adopt(preferences));
  }

  /** Read the stored preferences once, before the first window opens. */
  async load(): Promise<void> {
    this.#adopt(await this.#deps.store.preferences(), false);
  }

  /** The theme a page paints in now: the appearance's choice for the system's mode. */
  theme(): ThemeName {
    const system = this.#deps.nativeTheme.shouldUseDarkColors ? 'dark' : 'light';
    // With `themeSource` on the appearance's mode, the native theme reports the system's mode
    // only while that mode is `system`; `effectiveTheme` ignores it otherwise.
    return effectiveTheme(this.#current.appearance, system);
  }

  /** Whether runs notify when they finish, fail or pause. */
  notificationsEnabled(): boolean {
    return this.#current.notifications.enabled;
  }

  /** Answer one validated preferences request with the preferences as stored after it. */
  async answer(request: PreferencesRequest): Promise<PreferencesView> {
    switch (request.kind) {
      case 'preferences':
        return view(this.#current);
      case 'set-appearance':
        return view(await this.#update(current => ({...current, appearance: request.appearance})));
      case 'set-notifications':
        return view(
          await this.#update(current => ({...current, notifications: {enabled: request.enabled}})),
        );
      case 'archive':
        return view(
          await this.#update(current => withArchived(current, request.run, request.archived)),
        );
    }
  }

  #update(change: (current: Preferences) => Preferences): Promise<Preferences> {
    return this.#deps.store.updatePreferences(change);
  }

  #adopt(preferences: Preferences, announce = true): void {
    this.#current = preferences;
    this.#deps.nativeTheme.themeSource = preferences.appearance.mode;
    if (announce) this.#deps.push(view(preferences));
  }
}
