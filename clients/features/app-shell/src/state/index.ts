/**
 * The app shell's pure state: the sidebar's store and its rows, and which run notifications a
 * host's listing calls for.
 */
export {type NoticeKind, type RunNotice, runNotices} from './notices.js';
export {type RunRowStatus, statusWord} from './run-status.js';
export {
  liveRows,
  machineCounts,
  RECENT_ROWS,
  type RunRow,
  recentRows,
  type Screen,
  type ShellActions,
  SIDEBAR_EMPTY,
  type SidebarAction,
  type SidebarState,
  sidebarInvariants,
  sidebarStep,
} from './sidebar.js';
