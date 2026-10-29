/** The app shell's renderer entry: the window's rail and layout, and the placeholder screens. */

export {
  createSidebarStore,
  LIVE_DIGEST_REFRESH_MS,
  type ScheduleRefresh,
  type ShellBridge,
} from './effects/store.js';
export {AppShell, type RailBadges} from './ui/app-shell.js';
