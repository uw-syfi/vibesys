/**
 * The app shell's pure state: the sidebar's store and its rows, and which run notifications a
 * host's listing calls for.
 */
export {type NoticeKind, type RunNotice, runNotices} from './notices.js';
export {
  liveRows,
  machineCounts,
  RECENT_ROWS,
  type RunRow,
  type RunRowStatus,
  recentRows,
  type Screen,
  type ShellActions,
  SIDEBAR_EMPTY,
  type SidebarAction,
  type SidebarState,
  sidebarInvariants,
  sidebarStep,
  statusWord,
} from './sidebar.js';
