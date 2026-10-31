/**
 * The settings feature's welcome requests: read the preferences, change the appearance and the
 * notifications, and archive or restore a run. Each answers with the preferences as stored after
 * it, and every change is also pushed as the `preferences` event.
 */
import {parseRunRef, type RunRef} from '@vibesys/runs';
import {type Appearance, parseAppearance, type ThemeName} from '@vibesys/ui/tokens';
import type {WelcomeResult} from './types.js';
import type {ChannelOf} from './values.js';

export const SETTINGS_CHANNELS = {
  preferences: 'vibesys:welcome:preferences',
  setAppearance: 'vibesys:welcome:set-appearance',
  setNotifications: 'vibesys:welcome:set-notifications',
  archive: 'vibesys:welcome:archive',
} as const;

/**
 * The channel the welcome preload asks on, synchronously and before the page's scripts run, for what
 * the first paint needs (`FirstPaint`), so it is already in the stored theme and clear of the
 * window controls.
 */
export const FIRST_PAINT_CHANNEL = 'vibesys:welcome:first-paint';

/**
 * The channel on which the main process pushes the window's `ChromeInset` whenever it changes (the
 * window enters or leaves full screen), so the page re-lays itself out without a reload.
 */
export const CHROME_INSET_CHANNEL = 'vibesys:welcome:chrome-inset';

/**
 * What the window's own controls cover, in CSS pixels; all zero where there are none (full screen).
 * The page keeps the rail and its content clear of it and draws no strip for it.
 */
export interface ChromeInset {
  /** Width of the top-left corner the controls cover, with their margin (the macOS traffic lights). */
  readonly startWidth: number;
  /** Height of that corner: the room at the top of the rail the controls sit in. */
  readonly startHeight: number;
  /** Height of the strip across the top of the page the controls at the top right cover (Windows, Linux). */
  readonly endHeight: number;
}

/** What a page needs before its first paint. */
export interface FirstPaint {
  /** The stored theme, for the system's mode now. */
  readonly theme: ThemeName;
  readonly chromeInset: ChromeInset;
}

/** The preferences the views read: the appearance, notifications, and the runs archived. */
export interface PreferencesView {
  readonly appearance: Appearance;
  readonly notifications: {readonly enabled: boolean};
  readonly archived: readonly RunRef[];
}

/** The welcome bridge's requests the settings feature makes. */
export interface SettingsWelcomeBridge {
  preferences(): Promise<WelcomeResult<PreferencesView>>;
  setAppearance(appearance: Appearance): Promise<WelcomeResult<PreferencesView>>;
  setNotifications(enabled: boolean): Promise<WelcomeResult<PreferencesView>>;
  archive(run: RunRef, archived: boolean): Promise<WelcomeResult<PreferencesView>>;
}

export type SettingsRequest =
  | {readonly kind: 'preferences'}
  | {readonly kind: 'set-appearance'; readonly appearance: Appearance}
  | {readonly kind: 'set-notifications'; readonly enabled: boolean}
  | {readonly kind: 'archive'; readonly run: RunRef; readonly archived: boolean};

function flag(value: unknown, what: string): boolean {
  if (typeof value !== 'boolean') throw new Error(`${what} must be true or false`);
  return value;
}

/** The typed request on `channel` with `args`; throws naming the first invalid value. */
export function parseSettingsRequest(
  channel: ChannelOf<typeof SETTINGS_CHANNELS>,
  args: readonly unknown[],
): SettingsRequest {
  const [first, second] = args;
  switch (channel) {
    case SETTINGS_CHANNELS.preferences:
      return {kind: 'preferences'};
    case SETTINGS_CHANNELS.setAppearance:
      return {kind: 'set-appearance', appearance: parseAppearance(first, 'appearance')};
    case SETTINGS_CHANNELS.setNotifications:
      return {kind: 'set-notifications', enabled: flag(first, 'notifications')};
    case SETTINGS_CHANNELS.archive:
      return {kind: 'archive', run: parseRunRef(first, 'run'), archived: flag(second, 'archived')};
  }
}
