/**
 * The Settings screen's shell: its store, whose loads and saves go to the settings bridge, and the
 * system's appearance, observed from `prefers-color-scheme`.
 */
import {
  attempt,
  type PreferencesView,
  type SettingsWelcomeBridge,
  type WelcomeBridge,
  type WelcomeResult,
} from '@vibesys/ipc';
import {createStore, type Store} from '@vibesys/store';
import type {ThemeMode} from '@vibesys/ui/tokens';
import {
  initialSettings,
  type SettingsAction,
  type SettingsRequest,
  type SettingsState,
  settingsStep,
} from '../../state/index.js';

/** The bridge calls and pushes the Settings store uses. */
export type SettingsBridge = SettingsWelcomeBridge & Pick<WelcomeBridge, 'onPreferences'>;

/** What the store needs of the system's appearance. */
export interface SystemAppearance {
  readonly mode: ThemeMode;
  /** Hear every change of the system's mode. */
  onChange(listener: (mode: ThemeMode) => void): void;
}

/** The system's appearance in a page, from the `prefers-color-scheme` media query. */
export function pageAppearance(window: Pick<Window, 'matchMedia'>): SystemAppearance {
  const query = window.matchMedia('(prefers-color-scheme: dark)');
  return {
    mode: query.matches ? 'dark' : 'light',
    onChange: listener =>
      query.addEventListener('change', event => listener(event.matches ? 'dark' : 'light')),
  };
}

/** What a load or save answered, as the action the store folds. */
function replied(request: SettingsRequest, result: WelcomeResult<PreferencesView>): SettingsAction {
  const {token} = request;
  if (request.kind === 'load') {
    return result.ok
      ? {type: 'loaded', token, preferences: result.value}
      : {type: 'load-failed', token, error: result.error};
  }
  return result.ok
    ? {type: 'saved', token, preferences: result.value}
    : {type: 'save-failed', token, error: result.error};
}

function perform(
  bridge: SettingsBridge,
  request: SettingsRequest,
  dispatch: (action: SettingsAction) => void,
): void {
  const call = () =>
    request.kind === 'load'
      ? bridge.preferences()
      : request.change.kind === 'appearance'
        ? bridge.setAppearance(request.change.appearance)
        : bridge.setNotifications(request.change.enabled);
  void attempt(call).then(result => dispatch(replied(request, result)));
}

/**
 * The Settings store over `bridge`: it loads at once, follows the system's appearance and every
 * preferences push, and saves each change.
 */
export function createSettingsStore(
  bridge: SettingsBridge,
  system: SystemAppearance,
): Store<SettingsState, SettingsAction> {
  const initial = initialSettings(system.mode);
  const store = createStore(settingsStep, initial.state, (request, dispatch) =>
    perform(bridge, request, dispatch),
  );
  for (const request of initial.requests) perform(bridge, request, store.dispatch);
  system.onChange(mode => store.dispatch({type: 'os', mode}));
  bridge.onPreferences(event => store.dispatch({type: 'pushed', preferences: event.value}));
  return store;
}
