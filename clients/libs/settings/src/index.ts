/**
 * The app's settings: per-host checkouts (`hosts.json`) and the runs this user opened
 * (`recent.json`), validated, with `SettingsStore` as their one writer.
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
} from './host-settings.js';
export {
  type RecentFile,
  remember,
  rememberInstance,
  type StoredRecentRun,
} from './recent.js';
export {type HostSettingsSnapshot, SettingsStore} from './settings-store.js';
export {sshGlobSegmentMatches} from './ssh-pattern.js';
