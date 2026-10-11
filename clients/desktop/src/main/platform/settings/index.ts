/**
 * The app's settings: per-host checkouts (`hosts.json`) and the runs this user opened
 * (`recent.json`), validated, with `SettingsStore` as their one writer.
 */
export {
  settingsFor,
  sshConfigHosts,
  withHostSettings,
} from './host-settings.js';
export {type RecentFile, type RecentRun, remember} from './recent.js';
export {SettingsStore} from './settings-store.js';
