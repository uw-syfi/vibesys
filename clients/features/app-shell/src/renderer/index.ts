/** The app shell's renderer entry: the window's sidebar and layout, and the placeholder screens. */

export {
  bindSidebarShortcut,
  browserSidebarPreference,
  type KeyTarget,
  SIDEBAR_PREFERENCE_KEY,
  type SidebarPreference,
} from './effects/sidebar-visibility.js';
export {
  createSidebarStore,
  LIVE_DIGEST_REFRESH_MS,
  type ScheduleRefresh,
  type ShellBridge,
} from './effects/store.js';
export {AllRunsScreen} from './ui/all-runs-screen.js';
export {AppShell} from './ui/app-shell.js';
export {PlaceholderScreen, WORDMARK} from './ui/placeholders.js';
