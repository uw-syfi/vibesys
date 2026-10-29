/**
 * The app shell's pure state: the store of the runs the hosts list (listings, digests, the screen
 * shown) and the dashboard's rows over it, and which run notifications a host's listing calls for.
 */

export {projectRunGaps} from './all-runs.js';
export {type DashboardRow, dashboardRows, type RunLabel, runLabels} from './all-runs-rows.js';
export {type RunsKnown, runsKnown} from './home.js';
export {type NoticeKind, type RunNotice, runNotices} from './notices.js';
export {NOTICE_REASONS, type RecentNotice, recentNotice} from './recent-notice.js';
export {type Clock, formatStarted, formatWhen} from './recent-words.js';
export {type RunRowStatus, statusWord} from './run-status.js';
export {
  type DigestSlot,
  digestKey,
  initialSidebar,
  liveRows,
  RECENT_ROWS,
  type ReadDigest,
  type RowProblem,
  type RunRow,
  recentRows,
  runProjectRoot,
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
