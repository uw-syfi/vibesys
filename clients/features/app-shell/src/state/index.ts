/**
 * The app shell's pure state: the sidebar's store and its rows, and which run notifications a
 * host's listing calls for.
 */

export {type AllRunsGroup, allRunsGroups, type MachineRunsState} from './all-runs-rows.js';
export {type NoticeKind, type RunNotice, runNotices} from './notices.js';
export {NOTICE_REASONS, type RecentNotice, recentNotice} from './recent-notice.js';
export {type Clock, formatWhen} from './recent-words.js';
export {type RunRowStatus, statusWord} from './run-status.js';
export {
  type DigestSlot,
  digestKey,
  initialSidebar,
  liveMachineGroups,
  liveRows,
  type MachineRunGroup,
  RECENT_ROWS,
  type RowProblem,
  type RunRow,
  recentGroups,
  recentRows,
  type Screen,
  type ShellActions,
  SIDEBAR_EMPTY,
  type SidebarAction,
  type SidebarRequest,
  type SidebarState,
  type SidebarStep,
  sidebarInvariants,
  sidebarStep,
} from './sidebar.js';
export {
  isSidebarShortcut,
  parseSidebarShown,
  type ShortcutKey,
  sidebarShortcutLabel,
  storeSidebarShown,
} from './sidebar-visibility.js';
