/**
 * The app's settings: per-host checkouts (`hosts.json`), the runs this user opened
 * (`recent.json`) and the preferences (`preferences.json`), validated, with `SettingsStore` as
 * their one writer.
 */
// Permanent read-only decoders for retained JSON documents. Current writes use
// the protobuf codecs above; these functions never write their source files.
export {
  configuredMachines,
  decodeRetainedSettings,
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
  parsePreferences as decodeRetainedPreferences,
  withArchived,
} from './preferences.js';
export {
  parseRecent as decodeRetainedRecent,
  type RecentFile,
  remember,
  rememberInstance,
  type StoredRecentRun,
} from './recent.js';
export {
  decodePreferences,
  decodeRecent,
  decodeSettings,
  encodePreferences,
  encodeRecent,
  encodeSettings,
  settingsBinaryPath,
} from './settings-codec.js';
export {
  type HostSettingsSnapshot,
  type RecentSnapshot,
  SettingsStore,
} from './settings-store.js';
export {sshGlobSegmentMatches} from './ssh-pattern.js';
