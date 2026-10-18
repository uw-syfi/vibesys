/**
 * The app's settings: per-host checkouts (`hosts.json`), the runs this user opened
 * (`recent.json`) and the preferences (`preferences.json`), validated, with `SettingsStore` as
 * their one writer.
 */
export {
  type SshConfigDirective,
  SshConfigSyntaxError,
  type SshConfigSyntaxReason,
  settingsFor,
  sshConfigDirectives,
  sshConfigHosts,
  sshHostPatternsMatch,
  withHostSettings,
  withoutHost,
} from './host-settings.js';
export {
  DEFAULT_PREFERENCES,
  isArchived,
  type Preferences,
  parsePreferences,
  withArchived,
} from './preferences.js';
export {
  type RecentFile,
  remember,
  rememberInstance,
  type StoredRecentRun,
} from './recent.js';
export {
  type HostSettingsSnapshot,
  type RecentSnapshot,
  SettingsStore,
} from './settings-store.js';
export {sshGlobSegmentMatches} from './ssh-pattern.js';
