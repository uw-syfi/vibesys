/**
 * The Settings screen's store: a pure reducer over the stored preferences, the system's appearance,
 * and the user's changes. A change shows at once and asks to be saved under a fresh token; the
 * reply for the newest token, or a pushed change, is what is stored. A reply for an older token is
 * dropped, so a slow save cannot undo a newer choice.
 */
import type {PreferencesView} from '@vibesys/ipc';
import {
  type Appearance,
  effectiveTheme,
  THEMES,
  type ThemeMode,
  type ThemeName,
} from '@vibesys/ui/tokens';

/** A change the user asked for; each is saved as a whole. */
export type SettingsChange =
  | {readonly kind: 'appearance'; readonly appearance: Appearance}
  | {readonly kind: 'notifications'; readonly enabled: boolean};

export type SettingsState =
  | {readonly kind: 'loading'; readonly os: ThemeMode; readonly token: number}
  /** The read failed: it waits for the user's `retry`, or for a push. */
  | {
      readonly kind: 'failed';
      readonly os: ThemeMode;
      readonly token: number;
      readonly error: string;
    }
  | {
      readonly kind: 'ready';
      readonly os: ThemeMode;
      /** What the screen shows: the stored preferences with any unsaved change applied. */
      readonly shown: PreferencesView;
      /** The save awaited, if any. */
      readonly saving: {readonly token: number; readonly change: SettingsChange} | null;
      /** Why the last save failed, until the next change. */
      readonly problem: string | null;
      /** The last token issued, so each is new. */
      readonly lastToken: number;
    };

export type SettingsAction =
  | {readonly type: 'loaded'; readonly token: number; readonly preferences: PreferencesView}
  | {readonly type: 'load-failed'; readonly token: number; readonly error: string}
  | {readonly type: 'retry'}
  | {readonly type: 'os'; readonly mode: ThemeMode}
  | {readonly type: 'change'; readonly change: SettingsChange}
  | {readonly type: 'saved'; readonly token: number; readonly preferences: PreferencesView}
  | {readonly type: 'save-failed'; readonly token: number; readonly error: string}
  /** The settings store wrote new preferences (this screen's save, or another's). */
  | {readonly type: 'pushed'; readonly preferences: PreferencesView};

export type SettingsRequest =
  | {readonly kind: 'load'; readonly token: number}
  | {readonly kind: 'save'; readonly token: number; readonly change: SettingsChange};

export interface SettingsStep {
  readonly state: SettingsState;
  readonly requests: readonly SettingsRequest[];
}

/** The store before anything is read, with the system's appearance as `os`. */
export function initialSettings(os: ThemeMode): SettingsStep {
  return {state: {kind: 'loading', os, token: 1}, requests: [{kind: 'load', token: 1}]};
}

function applied(preferences: PreferencesView, change: SettingsChange): PreferencesView {
  return change.kind === 'appearance'
    ? {...preferences, appearance: change.appearance}
    : {...preferences, notifications: {enabled: change.enabled}};
}

function ready(os: ThemeMode, shown: PreferencesView, lastToken: number): SettingsState {
  return {kind: 'ready', os, shown, saving: null, problem: null, lastToken};
}

function stepReady(
  state: Extract<SettingsState, {kind: 'ready'}>,
  action: SettingsAction,
): SettingsStep {
  switch (action.type) {
    case 'os':
      return {state: {...state, os: action.mode}, requests: []};
    case 'change': {
      const token = state.lastToken + 1;
      return {
        state: {
          ...state,
          shown: applied(state.shown, action.change),
          saving: {token, change: action.change},
          problem: null,
          lastToken: token,
        },
        requests: [{kind: 'save', token, change: action.change}],
      };
    }
    case 'saved':
      if (state.saving?.token !== action.token) return {state, requests: []};
      return {state: {...state, shown: action.preferences, saving: null}, requests: []};
    case 'save-failed':
      if (state.saving?.token !== action.token) return {state, requests: []};
      return {state: {...state, saving: null, problem: action.error}, requests: []};
    case 'pushed': {
      // A save still awaited keeps showing its change over what was stored before it.
      const shown =
        state.saving === null
          ? action.preferences
          : applied(action.preferences, state.saving.change);
      return {state: {...state, shown}, requests: []};
    }
    case 'loaded':
    case 'load-failed':
    case 'retry':
      return {state, requests: []};
  }
}

/** The next state and the requests to make, for `action` in `state`. */
export function settingsStep(state: SettingsState, action: SettingsAction): SettingsStep {
  if (state.kind === 'ready') return stepReady(state, action);
  switch (action.type) {
    case 'os':
      return {state: {...state, os: action.mode}, requests: []};
    case 'loaded':
      if (state.kind !== 'loading' || action.token !== state.token) return {state, requests: []};
      return {state: ready(state.os, action.preferences, state.token), requests: []};
    case 'pushed':
      return {state: ready(state.os, action.preferences, state.token), requests: []};
    case 'load-failed':
      if (state.kind !== 'loading' || action.token !== state.token) return {state, requests: []};
      return {state: {...state, kind: 'failed', error: action.error}, requests: []};
    case 'retry': {
      if (state.kind !== 'failed') return {state, requests: []};
      const token = state.token + 1;
      return {state: {kind: 'loading', os: state.os, token}, requests: [{kind: 'load', token}]};
    }
    default:
      return {state, requests: []};
  }
}

/** The theme the app shows now: the appearance's choice for the system's mode. */
export function shownTheme(state: SettingsState, fallback: Appearance): ThemeName {
  return effectiveTheme(state.kind === 'ready' ? state.shown.appearance : fallback, state.os);
}

/** What must hold in every state; empty when all do. */
export function settingsInvariants(state: SettingsState): string[] {
  if (state.kind !== 'ready') return state.token > 0 ? [] : ['the load token is not positive'];
  const problems: string[] = [];
  const {appearance} = state.shown;
  if (THEMES[appearance.light].mode !== 'light') problems.push('the light theme is not light');
  if (THEMES[appearance.dark].mode !== 'dark') problems.push('the dark theme is not dark');
  if (state.saving !== null && state.saving.token !== state.lastToken) {
    problems.push('the awaited save is not the newest');
  }
  if (state.saving !== null && state.problem !== null) {
    problems.push('a failed save is shown while another is awaited');
  }
  return problems;
}
