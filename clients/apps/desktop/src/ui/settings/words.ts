/** What the settings screen says. Each closed set is named here, so a value added to one cannot reach the screen unnamed. */
import type {StatusTone} from '@vibesys/ui';
import type {AppearanceMode, ThemeName} from '@vibesys/ui/tokens';
import type {Freshness, ResourceConnection, SettingsTab} from '../../state/index.js';

export interface Word {
  readonly label: string;
  readonly tone: StatusTone;
}

export const TAB_WORD: Readonly<Record<SettingsTab, string>> = {
  appearance: 'Appearance',
  resources: 'Resources',
};

export const MODE_WORD: Readonly<Record<AppearanceMode, string>> = {
  system: 'System',
  light: 'Light',
  dark: 'Dark',
};

/** A theme's name is its token name with a capital: a theme added to the token table is named with no change here. */
export const themeWord = (theme: ThemeName): string =>
  `${theme.charAt(0).toUpperCase()}${theme.slice(1)}`;

export const CONNECTION_WORD: Readonly<Record<ResourceConnection, Word>> = {
  idle: {label: 'Idle', tone: 'neutral'},
  connecting: {label: 'Connecting', tone: 'neutral'},
  connected: {label: 'Connected', tone: 'ok'},
  reconnecting: {label: 'Reconnecting', tone: 'warn'},
  incompatible: {label: 'Incompatible', tone: 'err'},
  unreachable: {label: 'Unreachable', tone: 'err'},
};

export const FRESHNESS_WORD: Readonly<Record<Freshness, Word>> = {
  live: {label: 'Live', tone: 'live'},
  'catching-up': {label: 'Catching up', tone: 'neutral'},
  stale: {label: 'Stale', tone: 'warn'},
  empty: {label: 'Waiting', tone: 'neutral'},
  unavailable: {label: 'Unavailable', tone: 'err'},
};
