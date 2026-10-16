/**
 * The user's preferences (`preferences.json` in the app's user data), as pure, validated data:
 * the appearance (follow the system or always light or dark, and the light and dark theme), whether
 * runs notify when they finish, fail or pause, and the runs archived from Recent. Archive is the
 * app's own: it hides a run from Recent and changes nothing on the host.
 */
import {parseRunRef, type RunRef, sameRun} from '@vibesys/runs';
import {type Appearance, parseAppearance} from '@vibesys/ui/tokens';
import {object, onlyKeys, SettingsError} from './parse.js';

export interface Preferences {
  readonly version: 1;
  readonly appearance: Appearance;
  readonly notifications: {readonly enabled: boolean};
  readonly archived: readonly RunRef[];
}

export const DEFAULT_PREFERENCES: Preferences = {
  version: 1,
  appearance: {mode: 'system', light: 'paper', dark: 'ink'},
  notifications: {enabled: true},
  archived: [],
};

function settingsError<T>(parse: () => T): T {
  try {
    return parse();
  } catch (error) {
    throw new SettingsError((error as Error).message);
  }
}

/** Validate a `preferences.json` document, rejecting unknown keys with their path. */
export function parsePreferences(value: unknown): Preferences {
  const record = object(value, 'preferences');
  onlyKeys(record, ['version', 'appearance', 'notifications', 'archived'], 'preferences');
  if (record['version'] !== 1) throw new SettingsError('preferences.version must be 1');
  const notifications = object(record['notifications'], 'preferences.notifications');
  onlyKeys(notifications, ['enabled'], 'preferences.notifications');
  if (typeof notifications['enabled'] !== 'boolean') {
    throw new SettingsError('preferences.notifications.enabled must be true or false');
  }
  const archived = record['archived'];
  if (!Array.isArray(archived)) throw new SettingsError('preferences.archived must be a list');
  return {
    version: 1,
    appearance: settingsError(() =>
      parseAppearance(record['appearance'], 'preferences.appearance'),
    ),
    notifications: {enabled: notifications['enabled']},
    archived: archived.map((entry, index) =>
      settingsError(() => parseRunRef(entry, `preferences.archived[${index}]`)),
    ),
  };
}

/** `preferences` with `run` archived (once) or restored to Recent. */
export function withArchived(
  preferences: Preferences,
  run: RunRef,
  archived: boolean,
): Preferences {
  const others = preferences.archived.filter(entry => !sameRun(entry, run));
  return {...preferences, archived: archived ? [...others, run] : others};
}

/** Whether `run` is archived in `preferences`. */
export function isArchived(preferences: Preferences, run: RunRef): boolean {
  return preferences.archived.some(entry => sameRun(entry, run));
}
