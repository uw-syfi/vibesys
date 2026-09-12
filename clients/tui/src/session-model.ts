import type {
  ChatOptions,
  DesignFileChange,
  DesignRound,
  Diagnostic,
  HypothesisEntry,
  HypothesisRound,
  RunEvent,
  RunSnapshot,
} from '@vibesys/backend-client';
import {
  type ActiveAgentExecution,
  type ActiveExecutionCheckpoint,
  type AgentPhase,
  type ChatThread,
  type CoreDiagnostic,
  type CoreRunStatus,
  type CoreState,
  DEFAULT_CHAT_THREAD_ID,
  type ExecutionTodos,
  hasRunEnded,
  initialCoreState,
  latestDiagnosticChange,
  phasesForRound,
  type RoundState,
  reconcileActiveExecutions,
  reduceEvent,
  reduceEventBatch,
  reduceEventPrefix,
  reduceEventRebootstrap,
  reduceSnapshot,
  type TodoItem,
  type TranscriptEntry,
} from '@vibesys/core-state';
import {agentRuntimeLabel} from './ui/agent-runtime-label.js';
import {DEFAULT_THEME_NAME, THEME_NAMES, type ThemeName} from './ui/theme.js';

export interface SessionState {
  /** Pure projection of backend snapshots, events, and execution checkpoints. */
  readonly core: CoreState;
  /** False after the frontend loses a trustworthy backend event stream. */
  eventStreamAvailable: boolean;
  selectedRound: number | null;
  selectedAgentKind: string | null;
  /** Transcript entry the arrow keys are on, so a trace is readable without a mouse. */
  selectedEntryId: string | null;
  /** Todo row the arrow keys are on while the todo box holds focus. */
  selectedTodoIndex: number | null;
  /**
   * Which half of the round view the arrow keys act on. The round view is two
   * panes side by side, and both are navigable, so the keys have to belong to
   * one of them at a time.
   */
  roundFocus: RoundFocus;
  overlay: OverlayPanel | null;
  chatOpen: boolean;
  /** The thread the chat surfaces show and the composer submits to. */
  activeChatThreadId: string;
  /** The active thread's conversation, derived from `chatConversations`. */
  chatConversation: ConversationEntry[];
  /** Every thread's conversation (local user entries merged with replay). */
  chatConversations: Record<string, ConversationEntry[]>;
  /** True while the active thread awaits an answer; from `chatPendingThreads`. */
  chatPending: boolean;
  chatPendingThreads: Record<string, boolean>;
  /** Non-null while the composer's inline command menu is open. */
  chatMenu: ChatMenu | null;
  todosExpanded: boolean;
  themeName: ThemeName;
  experimentLog: ExperimentLogState | null;
  /**
   * Server-derived per-round design log: files each round changed and how its
   * stages concluded. Null until the first fetch lands; kept when a refresh
   * fails so the drill-down degrades to stale rather than empty.
   */
  designLog: DesignRound[] | null;
  /** Hypothesis summary between the experiment index and a round trajectory. */
  hypothesisDetail: HypothesisDetail | null;
  hypothesisScope: HypothesisScope | null;
  layout: LayoutState;
  /**
   * Whether the terminal is wide enough to carry the docked chat beside the
   * log. Measured by the renderer and reported in, because where a question's
   * answer is displayed has to agree with what is actually on screen: too
   * narrow to dock and the chat is the modal it has always been.
   */
  chatDockFits: boolean;
  /** Non-null while the theme list is open as a keyboard selection. */
  themePicker: ThemePicker | null;
  /** Root-level error state, independent of the active transcript or log view. */
  errorBanner: ErrorBannerState | null;
}

export type ErrorSeverity = 'recoverable' | 'fatal';
export type ErrorScope =
  | 'configuration'
  | 'invocation'
  | 'phase'
  | 'run'
  | 'protocol'
  | 'request'
  | 'transport'
  | 'input';

export interface ErrorBannerState {
  title: string;
  /** Human-facing summary, shown before structured detail and hint. */
  message: string;
  detail: string | null;
  hint: string | null;
  diagnosticId: string | null;
  severity: ErrorSeverity;
  scope: ErrorScope;
  agentKind: string | null;
  roundLabel: string | null;
  invocationId: string | null;
  /** Equivalent reports are folded into one banner. */
  count: number;
}

/** The agent graph on the left, or the transcript on the right. */
export type RoundFocus = 'rounds' | 'agents' | 'transcript';

/**
 * The experiment log is open when this is non-null. Selection is held as a
 * hypothesis id rather than a row index so a refresh that inserts rows keeps
 * the operator on the same hypothesis.
 */
export interface ExperimentLogState {
  entries: HypothesisEntry[];
  selectedId: string | null;
  /** The transient planning activity is selected instead of a recorded claim. */
  selectedActivity?: boolean;
  /** Round anchor retained while selected planning becomes a persisted hypothesis. */
  selectedActivityRound?: number | null;
  /** A recorded round that has no hypothesis entry is selected. */
  selectedUnownedRound?: number | null;
  pending: boolean;
  error: string | null;
}

/** UI-only navigation state for the selected hypothesis summary. */
export interface HypothesisDetail {
  entryKey: string;
  selectedRound: number | null;
}

/** A planning phase that has not produced a hypothesis record yet. */
export interface HypothesisPlanningActivity {
  stage: 'pre' | 'profile' | 'plan';
  roundNumber: number;
  startedAt?: string;
}

/** Every selectable item in the hypotheses-first index. */
export type ExperimentIndexItem =
  | {kind: 'activity'; key: 'activity'; activity: HypothesisPlanningActivity}
  | {kind: 'hypothesis'; key: string; entry: HypothesisEntry}
  | {kind: 'round'; key: `round-${number}`; roundNumber: number};

/**
 * The hypothesis whose rounds the operator opened. While this is set the
 * client shows the ordinary per-round trajectory, filtered to these rounds,
 * and the log table steps aside without losing its selection.
 */
export interface HypothesisScope {
  id: string;
  label: string;
  /**
   * The claim on its own, without the `· r1-r2` range `label` carries. The
   * header shows this: the range duplicates the rounds strip, and spending
   * header width on it pushed the title itself off the line.
   */
  title: string;
  rounds: number[];
  /** A single recorded round not yet associated with a hypothesis. */
  source?: 'hypothesis' | 'round';
}

/**
 * A visualization command's output, rendered beside the transcript rather than
 * over it. Content is pre-rendered text so the pane stays agnostic about which
 * command produced it and a new command needs no new layout code.
 */
export type PaneView = 'perf' | 'design';

export interface RightPane {
  view: PaneView;
  title: string;
  content: string;
  pending: boolean;
  error: string | null;
}

/**
 * Which column the pane keys act on. ``left`` is whatever holds the middle of
 * the row, the experiment log or the transcript; ``chat`` and ``right`` are the
 * panes beside it.
 */
export type PaneFocus = 'chat' | 'left' | 'right';

export interface LayoutState {
  /** null means no visualization pane: the left side has the rest of the row. */
  right: RightPane | null;
  focus: PaneFocus;
  /** The pane temporarily occupying the full content row, or null for the split layout. */
  zoomedPane: PaneId | null;
}

/**
 * Semantic pane identities, independent of their current screen position.
 *
 * `todos` is the expanded todo list. It is a strip rather than a column, but it
 * takes the arrow keys from the pane it opens over, so it is a pane for the
 * purpose the identities exist for: naming the one surface the keys are on.
 */
export type PaneId = 'agents' | 'chat' | 'experiments' | 'performance' | 'todos' | 'transcript';

export interface ThemePicker {
  selected: ThemeName;
}

/**
 * One row of the inline menu anchored to the chat composer. Only `model`,
 * `custom`, and `thread` rows are selectable; headers and notes are structure.
 */
export type ChatMenuRow =
  | {kind: 'header'; label: string}
  | {kind: 'note'; label: string}
  | {kind: 'model'; label: string; provider: string; model: string; isDefault: boolean}
  | {kind: 'custom'; label: string; provider: string}
  | {kind: 'thread'; label: string; detail: string; threadId: string; active: boolean};

/**
 * The chat composer's own selection surface, rendered adjacent to the composer
 * rather than as a dialog over the view. Rows are computed once when the menu
 * opens, so the renderer stays a projection of state and a frame test can read
 * exactly what an operator sees.
 *
 * The client enumerates nothing: `model` rows come from the backend's
 * `query.chat_options` response verbatim.
 */
export interface ChatMenu {
  kind: 'model' | 'resume';
  title: string;
  rows: ChatMenuRow[];
  /** Index of the highlighted row, or -1 while there is nothing to select. */
  selected: number;
  pending: boolean;
  error: string | null;
  /** Free text typed into each provider's custom entry, keyed by provider. */
  customModels: Record<string, string>;
}

/** The agent selection a new thread should inherit, or null for the run's. */
export interface ChatThreadSettings {
  provider: string;
  model: string;
}

export interface OverlayPanel {
  kind: 'detail' | 'help' | 'error';
  content: string;
}

export interface ConversationEntry {
  id: string;
  kind:
    | 'assistant'
    | 'user'
    | 'prompt'
    | 'analysis'
    | 'tool'
    | 'diagnostic'
    | 'subprocess'
    | 'status'
    | 'result';
  content: string;
  label?: string;
  tone?: 'normal' | 'success' | 'failure';
  agentKind?: string;
  roundLabel?: string;
  roundNumber?: number;
  turnId?: string;
  invocationId?: string;
  startsTurn?: boolean;
  toolCall?: string;
  toolResponse?: string;
  toolName?: string;
  toolCallId?: string;
  toolArguments?: Record<string, unknown>;
  toolResult?: TranscriptEntry['toolResult'];
}

export function initialSessionState(themeName: ThemeName = DEFAULT_THEME_NAME): SessionState {
  return {
    core: initialCoreState(),
    eventStreamAvailable: true,
    selectedRound: null,
    selectedAgentKind: null,
    selectedEntryId: null,
    selectedTodoIndex: null,
    roundFocus: 'transcript',
    overlay: null,
    chatOpen: false,
    activeChatThreadId: DEFAULT_CHAT_THREAD_ID,
    chatConversation: [],
    chatConversations: {},
    chatPending: false,
    chatPendingThreads: {},
    chatMenu: null,
    todosExpanded: false,
    themeName,
    // The experiment log is the landing view: a run's history reads as a short
    // list of claims before it reads as a long list of rounds.
    experimentLog: {entries: [], selectedId: null, pending: true, error: null},
    designLog: null,
    hypothesisDetail: null,
    hypothesisScope: null,
    layout: {right: null, focus: 'left', zoomedPane: null},
    // Docked until the renderer measures otherwise, so the landing view carries
    // the chat from the first frame rather than after a resize.
    chatDockFits: true,
    themePicker: null,
    errorBanner: null,
  };
}

/**
 * Rows are keyed by hypothesis identity rather than response position, so
 * selection survives refreshes even when the backend returns a different
 * order. The canonical index sorts the rows before presenting them.
 */
export function entryKey(entry: HypothesisEntry, index: number): string {
  return entry.identified === false
    ? `${entry.hypothesis_id}#${entry.first_round}`
    : entry.hypothesis_id || `#${index}`;
}

/**
 * True when the table itself is on screen rather than a hypothesis trajectory.
 * The chat does not enter into it: docked it is part of this view, and as a
 * modal it floats over it, so asking a question never swaps the table for the
 * per-round transcript underneath.
 */
export function experimentLogVisible(state: SessionState): boolean {
  return state.experimentLog !== null && state.hypothesisScope === null;
}

/**
 * The chat is docked on the landing view: it is part of that view rather than a
 * dialog over it, so a question never hides the table it is about. Inside a
 * hypothesis, and in a terminal too narrow for two columns, it stays the modal
 * it was.
 */
export function chatDocked(state: SessionState): boolean {
  return (
    state.chatDockFits &&
    state.experimentLog !== null &&
    state.hypothesisDetail === null &&
    state.hypothesisScope === null
  );
}

/** True when the docked chat is the thing on screen rather than the modal. */
export function chatPaneVisible(state: SessionState): boolean {
  return chatDocked(state) && !state.chatOpen;
}

export function chatPaneFocused(state: SessionState): boolean {
  return chatPaneVisible(state) && state.layout.focus === 'chat';
}

export function rightPaneFocused(state: SessionState): boolean {
  return state.layout.right !== null && state.layout.focus === 'right';
}

export function setChatDockFits(state: SessionState, fits: boolean): SessionState {
  if (state.chatDockFits === fits) return state;
  const layout =
    fits || state.layout.focus !== 'chat'
      ? state.layout
      : {...state.layout, focus: 'left' as const};
  return {...state, chatDockFits: fits, layout};
}

/**
 * What ``/chat`` does. Docked, the chat is already on screen, so opening it
 * means putting the pane keys on it rather than covering the table.
 */
export function openChat(state: SessionState): SessionState {
  if (chatDocked(state)) {
    return {...state, overlay: null, layout: {...state.layout, focus: 'chat'}};
  }
  return {...state, overlay: null, chatOpen: true};
}

/** Keeps the singular chat fields pointing at the active thread. */
function deriveActiveChat(state: SessionState): SessionState {
  const chatConversation = state.chatConversations[state.activeChatThreadId] ?? [];
  const chatPending = state.chatPendingThreads[state.activeChatThreadId] === true;
  if (state.chatConversation === chatConversation && state.chatPending === chatPending) {
    return state;
  }
  return {...state, chatConversation, chatPending};
}

/** Every thread the run knows about, the implicit default first. */
export function chatThreads(state: SessionState): ChatThread[] {
  return state.core.chatThreads;
}

/**
 * What the chat surfaces call one thread. Titles are backend-owned and arrive
 * through events; an untitled created thread reads as its harness and model.
 * The agent driver never appears: which driver backs a run is a deployment
 * detail, and every thread inherits the run's.
 */
export function chatThreadLabel(
  state: SessionState,
  threadId: string = state.activeChatThreadId,
): string {
  const thread = state.core.chatThreads.find(candidate => candidate.id === threadId);
  if (thread === undefined) return 'Experiment chat';
  if (thread.title) return thread.title;
  return agentRuntimeLabel(thread.provider, thread.model) ?? 'Experiment chat';
}

/**
 * What a chat surface puts in its header: the thread's name, and its runtime
 * when the name does not already spell one out. An operator switching threads
 * needs to see both which conversation this is and which agent answers it.
 */
export function chatThreadHeading(
  state: SessionState,
  threadId: string = state.activeChatThreadId,
): string {
  const label = chatThreadLabel(state, threadId);
  const runtime = chatThreadRuntimeLabel(state, threadId);
  return runtime === null || runtime === label ? label : `${label} · ${runtime}`;
}

/**
 * The active thread's runtime, e.g. `"Codex (GPT 5.5)"`. Null for a thread the
 * backend has not described, which is the default thread before any answer.
 */
export function chatThreadRuntimeLabel(
  state: SessionState,
  threadId: string = state.activeChatThreadId,
): string | null {
  const thread = state.core.chatThreads.find(candidate => candidate.id === threadId);
  if (thread === undefined) return null;
  return agentRuntimeLabel(thread.provider, thread.model);
}

/**
 * What a thread started from this one should inherit. Null means the thread
 * carries no selection of its own, so the backend resolves the run's.
 */
export function activeChatThreadSettings(state: SessionState): ChatThreadSettings | null {
  const thread = state.core.chatThreads.find(
    candidate => candidate.id === state.activeChatThreadId,
  );
  if (thread === undefined || thread.provider === null || thread.model === null) return null;
  return {provider: thread.provider, model: thread.model};
}

/** Makes one thread the chat surface's subject and puts the keys on the chat. */
export function switchChatThread(state: SessionState, threadId: string): SessionState {
  return openChat(deriveActiveChat({...state, activeChatThreadId: threadId, chatMenu: null}));
}

/** Applies one thread's conversation change and refreshes the derived fields. */
export function updateChatConversation(
  state: SessionState,
  threadId: string,
  update: (entries: ConversationEntry[]) => ConversationEntry[],
): SessionState {
  const entries = update(state.chatConversations[threadId] ?? []).slice(-500);
  return deriveActiveChat({
    ...state,
    chatConversations: {...state.chatConversations, [threadId]: entries},
  });
}

export function setChatThreadPending(
  state: SessionState,
  threadId: string,
  pending: boolean,
): SessionState {
  return deriveActiveChat({
    ...state,
    chatPendingThreads: {...state.chatPendingThreads, [threadId]: pending},
  });
}

/** `/resume`: the thread list, as an inline selection on the active thread. */
export function openChatResumeMenu(state: SessionState): SessionState {
  const rows: ChatMenuRow[] = state.core.chatThreads.map(thread => ({
    kind: 'thread' as const,
    label: chatThreadLabel(state, thread.id),
    detail: agentRuntimeLabel(thread.provider, thread.model) ?? 'run agent',
    threadId: thread.id,
    active: thread.id === state.activeChatThreadId,
  }));
  const active = rows.findIndex(row => row.kind === 'thread' && row.active);
  return {
    ...state,
    overlay: null,
    themePicker: null,
    chatMenu: {
      kind: 'resume',
      title: 'Chat threads',
      rows,
      selected: active === -1 ? firstSelectable(rows) : active,
      pending: false,
      error: null,
      customModels: {},
    },
  };
}

/** `/model`: opened empty, then filled by the backend's chat options. */
export function openChatModelMenu(state: SessionState): SessionState {
  return {
    ...state,
    overlay: null,
    themePicker: null,
    chatMenu: {
      kind: 'model',
      title: 'Harness and model',
      rows: [{kind: 'note', label: 'Loading options…'}],
      selected: -1,
      pending: true,
      error: null,
      customModels: {},
    },
  };
}

/**
 * Renders exactly what the backend returned: one group per provider it says is
 * valid, its models beneath, and a free-text entry per group for a model the
 * suggestion list does not carry.
 */
export function setChatModelMenuOptions(state: SessionState, options: ChatOptions): SessionState {
  const menu = state.chatMenu;
  if (menu === null || menu.kind !== 'model') return state;
  const rows: ChatMenuRow[] = [];
  for (const group of options.providers ?? []) {
    rows.push({kind: 'header', label: agentRuntimeLabel(group.provider, null) ?? group.provider});
    for (const option of group.models ?? []) {
      rows.push({
        kind: 'model',
        label: option.default ? `${option.model}  · run default` : option.model,
        provider: group.provider,
        model: option.model,
        isDefault: option.default === true,
      });
    }
    rows.push({kind: 'custom', label: 'custom model…', provider: group.provider});
  }
  if (rows.length === 0) rows.push({kind: 'note', label: 'This run offers no chat harness.'});
  return {...state, chatMenu: {...menu, rows, selected: firstSelectable(rows), pending: false}};
}

export function failChatMenu(state: SessionState, message: string): SessionState {
  const menu = state.chatMenu;
  if (menu === null) return state;
  return {
    ...state,
    chatMenu: {
      ...menu,
      rows: [{kind: 'note', label: message}],
      selected: -1,
      pending: false,
      error: message,
    },
  };
}

export function moveChatMenuSelection(state: SessionState, delta: number): SessionState {
  const menu = state.chatMenu;
  if (menu === null || delta === 0) return state;
  const step = delta > 0 ? 1 : -1;
  let selected = menu.selected;
  for (let remaining = Math.abs(delta); remaining > 0; remaining -= 1) {
    const next = nextSelectable(menu.rows, selected, step);
    if (next === selected) break;
    selected = next;
  }
  if (selected === menu.selected) return state;
  return {...state, chatMenu: {...menu, selected}};
}

export function closeChatMenu(state: SessionState): SessionState {
  if (state.chatMenu === null) return state;
  return {...state, chatMenu: null};
}

/** The row Enter acts on, or null while nothing selectable is highlighted. */
export function selectedChatMenuRow(state: SessionState): ChatMenuRow | null {
  const menu = state.chatMenu;
  if (menu === null || menu.selected < 0) return null;
  return menu.rows[menu.selected] ?? null;
}

/** Text typed into the highlighted custom entry, empty when none is. */
export function chatMenuCustomModel(state: SessionState): string {
  const row = selectedChatMenuRow(state);
  if (row === null || row.kind !== 'custom') return '';
  return state.chatMenu?.customModels[row.provider] ?? '';
}

export function setChatMenuCustomModel(state: SessionState, model: string): SessionState {
  const menu = state.chatMenu;
  const row = selectedChatMenuRow(state);
  if (menu === null || row === null || row.kind !== 'custom') return state;
  return {
    ...state,
    chatMenu: {...menu, customModels: {...menu.customModels, [row.provider]: model}},
  };
}

function isSelectable(row: ChatMenuRow): boolean {
  return row.kind === 'model' || row.kind === 'custom' || row.kind === 'thread';
}

function firstSelectable(rows: readonly ChatMenuRow[]): number {
  const index = rows.findIndex(isSelectable);
  return index;
}

/** The next selectable index in `step` direction, or `from` when there is none. */
function nextSelectable(rows: readonly ChatMenuRow[], from: number, step: number): number {
  for (let index = from + step; index >= 0 && index < rows.length; index += step) {
    const row = rows[index];
    if (row !== undefined && isSelectable(row)) return index;
  }
  return from < 0 ? firstSelectable(rows) : from;
}

export function openExperimentLog(state: SessionState): SessionState {
  const existing = state.experimentLog;
  return {
    ...state,
    overlay: null,
    chatOpen: false,
    hypothesisDetail: null,
    hypothesisScope: null,
    selectedRound: null,
    selectedAgentKind: null,
    experimentLog: existing ?? {entries: [], selectedId: null, pending: true, error: null},
  };
}

export function setExperiments(state: SessionState, entries: HypothesisEntry[]): SessionState {
  const log = state.experimentLog;
  if (log === null) return state;
  const activity = hypothesisPlanningActivity(state);
  const selectedActivityRound =
    log.selectedActivity === true
      ? (log.selectedActivityRound ?? activity?.roundNumber ?? null)
      : null;
  const orderedEntries = [...entries].sort(compareHypothesisEntries);
  const keys = orderedEntries.map(entryKey);
  const materializedActivity =
    selectedActivityRound === null
      ? undefined
      : orderedEntries.find(entry => scopeRounds(entry).includes(selectedActivityRound));
  // Keep the operator's row when it still exists; otherwise fall back to the
  // active hypothesis, then to the first row.
  const selectedId =
    materializedActivity !== undefined
      ? entryKeyFor(orderedEntries, materializedActivity)
      : log.selectedId !== null && keys.includes(log.selectedId)
        ? log.selectedId
        : (keys[orderedEntries.findIndex(entry => entry.active === true)] ?? keys[0] ?? null);
  const unownedRounds = unownedExperimentRounds(state, orderedEntries);
  const currentUnownedRound = log.selectedUnownedRound;
  const selectedUnownedRound =
    currentUnownedRound !== undefined &&
    currentUnownedRound !== null &&
    unownedRounds.includes(currentUnownedRound)
      ? currentUnownedRound
      : orderedEntries.length === 0
        ? (unownedRounds[0] ?? null)
        : null;
  const currentDetail = state.hypothesisDetail;
  const detailEntry =
    currentDetail === null
      ? undefined
      : orderedEntries.find((entry, index) => entryKey(entry, index) === currentDetail.entryKey);
  const detailRounds = detailEntry === undefined ? [] : scopeRounds(detailEntry);
  const hypothesisDetail =
    currentDetail === null || detailEntry === undefined
      ? null
      : {
          entryKey: currentDetail.entryKey,
          selectedRound:
            currentDetail.selectedRound !== null &&
            detailRounds.includes(currentDetail.selectedRound)
              ? currentDetail.selectedRound
              : (detailRounds.at(-1) ?? null),
        };
  return {
    ...state,
    hypothesisDetail,
    experimentLog: {
      ...log,
      entries: orderedEntries,
      selectedId,
      selectedActivity: materializedActivity === undefined && log.selectedActivity === true,
      selectedActivityRound: materializedActivity === undefined ? selectedActivityRound : null,
      selectedUnownedRound,
      pending: false,
      error: null,
    },
  };
}

export function failExperiments(state: SessionState, error: string): SessionState {
  const log = state.experimentLog;
  if (log === null) return state;
  return {...state, experimentLog: {...log, pending: false, error}};
}

export function setDesignLog(state: SessionState, rounds: DesignRound[]): SessionState {
  return {...state, designLog: rounds};
}

/** The design entry for one round, or null before the log has loaded it. */
export function designRoundFor(
  state: SessionState,
  roundNumber: number | null,
): DesignRound | null {
  if (roundNumber === null || state.designLog === null) return null;
  return state.designLog.find(round => round.round === roundNumber) ?? null;
}

/**
 * The experiment log's record for one round, or null before the log has it.
 * Every stage fact about a round, including its measured delta, lives here:
 * the design log carries only the file list, so a consumer that wants an
 * outcome reads this rather than the design record.
 */
export function hypothesisRoundFor(
  state: SessionState,
  roundNumber: number | null,
): HypothesisRound | null {
  if (roundNumber === null) return null;
  for (const entry of state.experimentLog?.entries ?? []) {
    const record = entry.rounds?.find(candidate => candidate.round === roundNumber);
    if (record !== undefined) return record;
  }
  return null;
}

/**
 * One round as both surfaces know it: the experiment log owns every stage
 * fact, the design log owns only the file list. The join is by round number,
 * which is the identity both fetches agree on, so the two can never describe
 * the same round differently.
 */
export interface DesignRoundView {
  round: number;
  files: DesignFileChange[] | null;
  hypothesisId: string | null;
  title: string | null;
  /** The experiment log's row for this round, absent for a round it lost. */
  record: HypothesisRound | null;
}

export function designRoundViews(
  designLog: readonly DesignRound[],
  entries: readonly HypothesisEntry[],
): DesignRoundView[] {
  const owners = new Map<number, {entry: HypothesisEntry; record: HypothesisRound}>();
  for (const entry of entries) {
    for (const record of entry.rounds ?? []) owners.set(record.round, {entry, record});
  }
  return designLog.map(design => {
    const owner = owners.get(design.round) ?? null;
    return {
      round: design.round,
      files: design.files ?? null,
      hypothesisId: owner?.entry.hypothesis_id ?? null,
      title: owner?.entry.title ?? owner?.entry.claim ?? null,
      record: owner?.record ?? null,
    };
  });
}

export function moveExperimentSelection(state: SessionState, delta: number): SessionState {
  const log = state.experimentLog;
  if (log === null || state.hypothesisDetail !== null || state.hypothesisScope !== null)
    return state;
  const items = experimentIndexItems(state);
  if (items.length === 0) return state;
  const selected = selectedExperimentIndexItem(state);
  const current = selected === null ? -1 : items.findIndex(item => item.key === selected.key);
  const index = current === -1 ? 0 : Math.min(items.length - 1, Math.max(0, current + delta));
  return selectExperimentIndexItem(state, items[index]);
}

/** Open one hypothesis summary without entering any of its round trajectories. */
export function openHypothesisDetail(state: SessionState, requestedKey?: string): SessionState {
  const log = state.experimentLog;
  if (log === null || state.hypothesisScope !== null) return state;
  const key = requestedKey ?? log.selectedId;
  if (key === null) return state;
  const entry = log.entries.find((candidate, index) => entryKey(candidate, index) === key);
  if (entry === undefined) return state;
  const rounds = scopeRounds(entry);
  return {
    ...state,
    overlay: null,
    chatOpen: false,
    layout: {right: null, focus: 'left', zoomedPane: null},
    experimentLog: {
      ...log,
      selectedId: key,
      selectedActivity: false,
      selectedActivityRound: null,
      selectedUnownedRound: null,
    },
    hypothesisDetail: {entryKey: key, selectedRound: rounds.at(-1) ?? null},
  };
}

/** The durable hypothesis selected for the summary view. */
export function detailedHypothesis(state: SessionState): HypothesisEntry | null {
  const detail = state.hypothesisDetail;
  const entries = state.experimentLog?.entries ?? [];
  if (detail === null) return null;
  return entries.find((entry, index) => entryKey(entry, index) === detail.entryKey) ?? null;
}

/** Move the round cursor within the open hypothesis summary. */
export function moveHypothesisRoundSelection(state: SessionState, delta: number): SessionState {
  const detail = state.hypothesisDetail;
  const entry = detailedHypothesis(state);
  if (detail === null || entry === null || state.hypothesisScope !== null) return state;
  const rounds = scopeRounds(entry);
  if (rounds.length === 0) return state;
  const current = detail.selectedRound === null ? -1 : rounds.indexOf(detail.selectedRound);
  const index = current === -1 ? 0 : Math.min(rounds.length - 1, Math.max(0, current + delta));
  return {...state, hypothesisDetail: {...detail, selectedRound: rounds[index] ?? null}};
}

/** Return from a hypothesis summary to the experiment index. */
export function leaveHypothesisDetail(state: SessionState): SessionState {
  if (state.hypothesisDetail === null || state.hypothesisScope !== null) return state;
  return {...state, hypothesisDetail: null};
}

/** Selects the current planning work so Enter opens its live agent turns. */
export function selectExperimentActivity(state: SessionState): SessionState {
  const log = state.experimentLog;
  if (
    log === null ||
    state.hypothesisDetail !== null ||
    state.hypothesisScope !== null ||
    hypothesisPlanningActivity(state) === null
  ) {
    return state;
  }
  const activity = hypothesisPlanningActivity(state);
  return {
    ...state,
    experimentLog: {
      ...log,
      selectedActivity: true,
      selectedActivityRound: activity?.roundNumber ?? null,
      selectedUnownedRound: null,
    },
  };
}

/**
 * Advances the experiment navigation by one level: index to hypothesis
 * summary, then hypothesis summary to its selected round trajectory.
 */
export function enterExperimentDrilldown(state: SessionState): SessionState {
  if (state.hypothesisDetail !== null) {
    const roundNumber = state.hypothesisDetail.selectedRound;
    return roundNumber === null ? state : (enterExperimentRound(state, roundNumber) ?? state);
  }
  const activity = hypothesisPlanningActivity(state);
  if (
    activity !== null &&
    (state.experimentLog?.selectedActivity === true ||
      (state.experimentLog?.entries.length === 0 &&
        (state.experimentLog?.selectedUnownedRound ?? null) === null))
  ) {
    return enterUnownedExperimentRound(state, activity.roundNumber) ?? state;
  }
  const selectedRound =
    state.experimentLog?.selectedUnownedRound ??
    (selectedExperiment(state) === null ? unownedExperimentRounds(state)[0] : undefined);
  if (selectedRound !== undefined && selectedRound !== null) {
    return enterUnownedExperimentRound(state, selectedRound) ?? state;
  }
  const entry = selectedExperiment(state);
  if (entry === null || state.hypothesisScope !== null) return state;
  return openHypothesisDetail(state);
}

/** Leaves a trajectory for its hypothesis summary, preserving the round cursor. */
export function leaveExperimentDrilldown(state: SessionState): SessionState {
  if (state.hypothesisScope === null) return state;
  return {...state, hypothesisScope: null, selectedRound: null, selectedAgentKind: null};
}

/**
 * Opens the hypothesis that owns a round number, landing on that round rather
 * than on the whole trajectory. Returns null when no hypothesis claims it, so
 * the caller can report the round rather than silently doing nothing.
 */
export function enterExperimentRound(
  state: SessionState,
  roundNumber: number,
): SessionState | null {
  const entries = state.experimentLog?.entries ?? [];
  const entryIndex = entries.findIndex(candidate => scopeRounds(candidate).includes(roundNumber));
  const entry = entries[entryIndex];
  if (entry === undefined) return null;
  const entryKeyValue = entryKey(entry, entryIndex);
  const scoped: SessionState = {
    ...state,
    overlay: null,
    chatOpen: false,
    layout: {right: null, focus: 'left', zoomedPane: null},
    hypothesisScope: {
      id: entry.hypothesis_id,
      label: hypothesisLabel(entry),
      title: hypothesisTitle(entry),
      rounds: scopeRounds(entry),
      source: 'hypothesis',
    },
    selectedRound: roundNumber,
    selectedAgentKind: null,
    selectedEntryId: null,
    hypothesisDetail: {entryKey: entryKeyValue, selectedRound: roundNumber},
    experimentLog:
      state.experimentLog === null ? null : {...state.experimentLog, selectedId: entryKeyValue},
  };
  return scoped;
}

/**
 * Opens a recorded round that is not currently indexed by a hypothesis. This
 * is intentionally derived only from observed rounds, never from the planned
 * round count, so an empty future round is not presented as historical work.
 */
export function enterUnownedExperimentRound(
  state: SessionState,
  roundNumber: number,
): SessionState | null {
  if (!state.core.rounds.some(round => round.number === roundNumber)) return null;
  return {
    ...state,
    overlay: null,
    chatOpen: false,
    layout: {right: null, focus: 'left', zoomedPane: null},
    hypothesisDetail: null,
    hypothesisScope: {
      id: `round-${roundNumber}`,
      label: `Round ${roundNumber}`,
      title: `Round ${roundNumber}`,
      rounds: [roundNumber],
      source: 'round',
    },
    selectedRound: roundNumber,
    selectedAgentKind: null,
    selectedEntryId: null,
  };
}

function entryKeyFor(entries: HypothesisEntry[], entry: HypothesisEntry): string | null {
  const index = entries.indexOf(entry);
  return index === -1 ? null : entryKey(entry, index);
}

function scopeRounds(entry: HypothesisEntry): number[] {
  const listed = (entry.rounds ?? []).map(round => round.round);
  if (listed.length > 0) return [...listed].sort((a, b) => a - b);
  // A record can summarize a continuation before its per-round outcomes have
  // been persisted. Its declared range is still the server's ownership claim.
  if (entry.first_round <= 0 || entry.last_round < entry.first_round) return [];
  return Array.from(
    {length: Math.max(0, entry.last_round - entry.first_round + 1)},
    (_, index) => entry.first_round + index,
  );
}

/** Ordered round identities owned by a hypothesis, including legacy range-only records. */
export function hypothesisRoundNumbers(entry: HypothesisEntry): number[] {
  return scopeRounds(entry);
}

/** The claim itself, falling back to its id when the record carries no title. */
function hypothesisTitle(entry: HypothesisEntry): string {
  return entry.title ?? entry.hypothesis_id;
}

function hypothesisLabel(entry: HypothesisEntry): string {
  const range =
    entry.first_round === entry.last_round
      ? `r${entry.first_round}`
      : `r${entry.first_round}-${entry.last_round}`;
  return `${hypothesisTitle(entry)} · ${range}`;
}

export function selectedExperiment(state: SessionState): HypothesisEntry | null {
  const log = state.experimentLog;
  if (log === null || log.selectedId === null) return null;
  const index = log.entries.map(entryKey).indexOf(log.selectedId);
  return index === -1 ? null : (log.entries[index] ?? null);
}

/**
 * Observed numeric rounds not represented by any hypothesis record. Events
 * without a numeric round label remain in the run transcript but cannot be
 * honestly assigned an index row.
 */
export function unownedExperimentRounds(
  state: SessionState,
  entries: HypothesisEntry[] = state.experimentLog?.entries ?? [],
): number[] {
  const owned = new Set(entries.flatMap(scopeRounds));
  const planningRound = hypothesisPlanningActivity(state)?.roundNumber;
  return state.core.rounds
    .filter(
      round =>
        !owned.has(round.number) && round.number !== planningRound && round.status !== 'planned',
    )
    .map(round => round.number)
    .sort((left, right) => left - right);
}

/**
 * The canonical visual index. Historical work is chronological regardless of
 * server response order, and transient planning is always the newest item.
 */
export function experimentIndexItems(state: SessionState): ExperimentIndexItem[] {
  const history: ExperimentIndexItem[] = [];
  for (const [index, entry] of (state.experimentLog?.entries ?? []).entries()) {
    history.push({kind: 'hypothesis', key: entryKey(entry, index), entry});
  }
  for (const roundNumber of unownedExperimentRounds(state)) {
    history.push({kind: 'round', key: `round-${roundNumber}`, roundNumber});
  }
  history.sort((left, right) => experimentItemRound(left) - experimentItemRound(right));
  const activity = hypothesisPlanningActivity(state);
  return activity === null ? history : [...history, {kind: 'activity', key: 'activity', activity}];
}

function experimentItemRound(item: ExperimentIndexItem): number {
  if (item.kind === 'hypothesis') return item.entry.first_round;
  if (item.kind === 'round') return item.roundNumber;
  return item.activity.roundNumber;
}

function compareHypothesisEntries(left: HypothesisEntry, right: HypothesisEntry): number {
  return (
    left.first_round - right.first_round ||
    left.last_round - right.last_round ||
    left.hypothesis_id.localeCompare(right.hypothesis_id)
  );
}

export function selectedExperimentIndexItem(state: SessionState): ExperimentIndexItem | null {
  const log = state.experimentLog;
  if (log === null) return null;
  const items = experimentIndexItems(state);
  if (log.selectedActivity === true) {
    const activity = items.find(item => item.kind === 'activity');
    if (activity !== undefined) return activity;
  }
  if (log.selectedUnownedRound !== undefined && log.selectedUnownedRound !== null) {
    return (
      items.find(item => item.kind === 'round' && item.roundNumber === log.selectedUnownedRound) ??
      null
    );
  }
  return log.selectedId === null
    ? null
    : (items.find(item => item.kind === 'hypothesis' && item.key === log.selectedId) ?? null);
}

function selectExperimentIndexItem(
  state: SessionState,
  item: ExperimentIndexItem | undefined,
): SessionState {
  const log = state.experimentLog;
  if (log === null || item === undefined) return state;
  if (item.kind === 'activity')
    return {
      ...state,
      experimentLog: {
        ...log,
        selectedActivity: true,
        selectedActivityRound: item.activity.roundNumber,
        selectedUnownedRound: null,
      },
    };
  if (item.kind === 'round') {
    return {
      ...state,
      experimentLog: {
        ...log,
        selectedActivity: false,
        selectedActivityRound: null,
        selectedUnownedRound: item.roundNumber,
      },
    };
  }
  return {
    ...state,
    experimentLog: {
      ...log,
      selectedId: item.key,
      selectedActivity: false,
      selectedActivityRound: null,
      selectedUnownedRound: null,
    },
  };
}

/**
 * The loop publishes these labels before it writes a hypothesis record. Keep
 * this derived from active, named phases: an old phase must never make the
 * experiment log look live, and unrelated profiler work must not be presented
 * as hypothesis planning.
 */
export function hypothesisPlanningActivity(state: SessionState): HypothesisPlanningActivity | null {
  if (hasRunEnded(state.core) || state.experimentLog === null) return null;
  const phase = [...state.core.phases]
    .reverse()
    .find(
      candidate =>
        candidate.status === 'active' && planningStage(candidate.kind, candidate.roundLabel),
    );
  if (phase === undefined || phase.roundNumber === null) return null;
  const roundNumber = phase.roundNumber;
  if (state.experimentLog.entries.some(entry => scopeRounds(entry).includes(roundNumber)))
    return null;
  const stage = planningStage(phase.kind, phase.roundLabel);
  if (stage === null) return null;
  const startedAt = earliestPlanningStartedAt(state.core.phases, roundNumber);
  return {
    stage,
    roundNumber,
    ...(startedAt === undefined ? {} : {startedAt}),
  };
}

function earliestPlanningStartedAt(phases: AgentPhase[], roundNumber: number): string | undefined {
  const starts = phases
    .filter(
      phase => phase.roundNumber === roundNumber && planningStage(phase.kind, phase.roundLabel),
    )
    .flatMap(phase =>
      phase.startedAt === undefined
        ? []
        : [[phase.startedAt, Date.parse(phase.startedAt)] as const],
    )
    .filter(([, timestamp]) => Number.isFinite(timestamp));
  if (starts.length === 0) return undefined;
  return starts.reduce((earliest, candidate) =>
    candidate[1] < earliest[1] ? candidate : earliest,
  )[0];
}

function planningStage(
  agentKind: string,
  roundLabel: string | null,
): HypothesisPlanningActivity['stage'] | null {
  if (roundLabel === null) return null;
  if (agentKind === 'orchestrator' && /^round-\d+-pre$/.test(roundLabel)) return 'pre';
  if (agentKind === 'profiler' && /^round-\d+-profiler$/.test(roundLabel)) return 'profile';
  // A plan the framework reprompted carries `round-N-retry-K-plan`. It is the
  // same planning stage, produced by the same role for the same round, so it
  // must not fall out of the planning activity just because the first attempt
  // was rejected.
  if (agentKind === 'orchestrator' && /^round-\d+(?:-retry-\d+)?-plan$/.test(roundLabel)) {
    return 'plan';
  }
  return null;
}

export const PANE_TITLES: Record<PaneView, string> = {
  perf: 'Performance',
  design: 'Design changes',
};

/**
 * Opens or retargets the right pane. A second visualization command replaces
 * the pane's contents rather than stacking, which is why the view is set here
 * rather than pushed onto anything.
 */
export function openPane(state: SessionState, view: PaneView): SessionState {
  const existing = state.layout.right;
  return {
    ...state,
    overlay: null,
    layout: {
      right: {
        view,
        title: PANE_TITLES[view],
        // Keep the old content while the new query is in flight only when the
        // pane is not changing view, so the pane never shows one command's
        // output under another command's title.
        content: existing !== null && existing.view === view ? existing.content : '',
        pending: true,
        error: null,
      },
      focus: 'right',
      zoomedPane: state.layout.zoomedPane,
    },
  };
}

export function setPaneContent(state: SessionState, view: PaneView, content: string): SessionState {
  const right = state.layout.right;
  // A slower response for a pane the operator has since replaced or closed
  // must not overwrite what is on screen now.
  if (right === null || right.view !== view) return state;
  return {
    ...state,
    layout: {...state.layout, right: {...right, content, pending: false, error: null}},
  };
}

export function failPane(state: SessionState, view: PaneView, error: string): SessionState {
  const right = state.layout.right;
  if (right === null || right.view !== view) return state;
  return {...state, layout: {...state.layout, right: {...right, pending: false, error}}};
}

export function closePane(state: SessionState): SessionState {
  if (state.layout.right === null) return state;
  return {...state, layout: {right: null, focus: 'left', zoomedPane: null}};
}

/**
 * Moves the pane keys one column to the right, wrapping. Only the columns
 * actually on screen take part, so the operator never lands on a pane they
 * cannot see.
 */
export function cyclePaneFocus(state: SessionState): SessionState {
  if (state.layout.zoomedPane !== null) return state;
  const order = visiblePaneOrder(state);
  if (order.length < 2) return state;
  const next = order[(order.indexOf(state.layout.focus) + 1) % order.length] ?? 'left';
  return {...state, layout: {...state.layout, focus: next}};
}

/**
 * Focus has to name a column that is actually on screen. A pane can close, or a
 * view can change, while the keys still point at it, and every keystroke then
 * goes to a surface that is not there: the client looks frozen. Called on every
 * state change, so focus can never be left pointing at nothing.
 */
export function normalizeFocus(state: SessionState): SessionState {
  const order = visiblePaneOrder(state);
  const focus = order.includes(state.layout.focus) ? state.layout.focus : 'left';
  const zoomedPane =
    state.layout.zoomedPane === null ||
    visiblePaneIds({...state, layout: {...state.layout, focus}}).includes(state.layout.zoomedPane)
      ? state.layout.zoomedPane
      : null;
  if (focus === state.layout.focus && zoomedPane === state.layout.zoomedPane) return state;
  return {...state, layout: {...state.layout, focus, zoomedPane}};
}

/** Escape from a round view: close whatever is layered over it, all of it. */
export function closeOverlays(state: SessionState): SessionState {
  if (state.layout.right === null && !state.chatOpen && state.overlay === null) return state;
  return {
    ...state,
    overlay: null,
    chatOpen: false,
    layout: {right: null, focus: 'left', zoomedPane: null},
  };
}

export function focusPane(state: SessionState, focus: PaneFocus): SessionState {
  if (state.layout.focus === focus) return state;
  if (!visiblePaneOrder(state).includes(focus)) return state;
  return {...state, layout: {...state.layout, focus}};
}

/**
 * True while the expanded todo list is on screen and taking the arrow keys.
 *
 * `keybindings` routes Up and Down to the list for as long as it is open, so
 * the list rather than the pane it opened over is the surface those keys are
 * on. Derived from the toggle rather than stored beside it: collapsing the list
 * hands the keys back to whichever pane held them, with nothing to restore.
 *
 * The strip is drawn whenever the visible agent has todos, including under a
 * zoomed pane, so this is the whole condition: an empty list is not on screen
 * and cannot hold the keys.
 */
export function todoListFocused(state: SessionState): boolean {
  if (experimentLogVisible(state) || !state.todosExpanded) return false;
  return visibleTodos(state).length > 0;
}

/**
 * The semantic pane currently receiving pane navigation keys, and the single
 * authority for the focus treatment: every pane view compares its own `PaneId`
 * against this, so at most one of them can be lit at a time. Surfaces that are
 * not panes, the command box above all, never wear that treatment, and neither
 * does a box nested inside a pane that already wears it.
 */
export function focusedPane(state: SessionState): PaneId {
  if (experimentLogVisible(state)) {
    if (state.layout.focus === 'chat' && chatPaneVisible(state)) return 'chat';
    if (state.layout.focus === 'right' && state.layout.right !== null) return 'performance';
    return 'experiments';
  }
  // The expanded todo list takes the arrow keys from whichever pane held them,
  // so it holds the focus until it closes. Ahead of the panes below because the
  // keys reach it first.
  if (todoListFocused(state)) return 'todos';
  // Opening a visualization replaces the agent summary in the left column
  // with the transcript. Keep roundFocus intact so closing the visualization
  // can restore it, but never report the hidden Agents pane as focused.
  if (state.layout.right !== null) {
    return state.layout.focus === 'right' ? 'performance' : 'transcript';
  }
  // The rounds rail is a selector on the left of the round view, not one of the
  // zoomable content panes, so it reports as the agents side for pane-level
  // focus: no content pane lights its border, and F4 zooms the agents pane
  // rather than a rail that has nothing to enlarge. The rail draws its own
  // focus border from `roundFocus` directly.
  return state.roundFocus === 'rounds' ? 'agents' : state.roundFocus;
}

/**
 * Gives the focused pane the content row, or restores the existing split.
 * Pane content and selection stay in their owning models; zoom only changes
 * presentation, so toggling cannot replace or reconstruct pane state.
 */
export function togglePaneZoom(state: SessionState): SessionState {
  if (state.layout.zoomedPane !== null) {
    return {...state, layout: {...state.layout, zoomedPane: null}};
  }
  const pane = focusedPane(state);
  // Zoom hands the content row to a column. The todo list is a strip as tall as
  // its own contents, so the row has nothing to give it: leave the layout as it
  // is rather than clearing the screen around a five-row box.
  if (pane === 'todos') return state;
  return {...state, layout: {...state.layout, zoomedPane: pane}};
}

/**
 * Every pane the content row can be given to in the active view. The expanded
 * todo list is deliberately absent: it can hold the keys, but not the row.
 */
export function visiblePaneIds(state: SessionState): PaneId[] {
  if (experimentLogVisible(state)) {
    return [
      ...(chatPaneVisible(state) ? (['chat'] as const) : []),
      'experiments',
      ...(state.layout.right !== null ? (['performance'] as const) : []),
    ];
  }
  return state.layout.right === null ? ['agents', 'transcript'] : ['transcript', 'performance'];
}

function visiblePaneOrder(state: SessionState): PaneFocus[] {
  return [
    ...(chatPaneVisible(state) ? (['chat'] as const) : []),
    'left' as const,
    ...(state.layout.right !== null ? (['right'] as const) : []),
  ];
}

export function setTheme(state: SessionState, themeName: ThemeName): SessionState {
  // Applying the theme that is already active is still an answer to the
  // picker, so the picker closes either way.
  if (state.themeName === themeName) {
    return state.themePicker === null ? state : {...state, themePicker: null};
  }
  return {...state, themeName, overlay: null, themePicker: null};
}

/** Opens the theme list as a selection, starting on the active theme. */
export function openThemePicker(state: SessionState): SessionState {
  return {...state, overlay: null, themePicker: {selected: state.themeName}};
}

/**
 * Moves the highlighted theme. The list has ends rather than a cycle, so the
 * selection clamps instead of wrapping.
 */
export function moveThemeSelection(state: SessionState, delta: number): SessionState {
  const picker = state.themePicker;
  if (picker === null) return state;
  const current = THEME_NAMES.indexOf(picker.selected);
  const index = Math.min(THEME_NAMES.length - 1, Math.max(0, current + delta));
  const selected = THEME_NAMES[index];
  if (selected === undefined || selected === picker.selected) return state;
  return {...state, themePicker: {selected}};
}

/** Closes the picker, leaving the theme as it was when it opened. */
export function closeThemePicker(state: SessionState): SessionState {
  if (state.themePicker === null) return state;
  return {...state, themePicker: null};
}

export function applySnapshot(state: SessionState, snapshot: RunSnapshot): SessionState {
  const core = reduceSnapshot(state.core, snapshot);
  return core === state.core ? state : {...state, core};
}

/** Record transport health as frontend state without rewriting backend-derived facts. */
export function markEventStreamUnavailable(state: SessionState): SessionState {
  return state.eventStreamAvailable ? {...state, eventStreamAvailable: false} : state;
}

/**
 * Record recovered transport health after a successful resubscribe. The
 * transport banner describes a condition that no longer holds, so it retires
 * with the outage; banners from other scopes stay untouched.
 */
export function markEventStreamAvailable(state: SessionState): SessionState {
  const errorBanner = state.errorBanner?.scope === 'transport' ? null : state.errorBanner;
  if (state.eventStreamAvailable && errorBanner === state.errorBanner) return state;
  return {...state, eventStreamAvailable: true, errorBanner};
}

/** Active work is presentable only while its source stream remains trustworthy. */
export function visibleActiveExecutions(state: SessionState): ActiveAgentExecution[] {
  if (!state.eventStreamAvailable) return [];
  const roundNumber = visibleRoundNumber(state);
  return Object.values(state.core.activeExecutions).filter(
    execution =>
      (roundNumber === null || execution.roundNumber === roundNumber) &&
      (state.selectedAgentKind === null || execution.agentKind === state.selectedAgentKind),
  );
}

/** Replace activity with the backend checkpoint without advancing the event replay cursor. */
export function applyActiveExecutionCheckpoint(
  state: SessionState,
  executions: ActiveExecutionCheckpoint,
  throughSequence?: number,
): SessionState {
  const core = reconcileActiveExecutions(state.core, executions, throughSequence);
  return core === state.core ? state : {...state, core};
}

export function applyEvent(state: SessionState, event: RunEvent): SessionState {
  const core = reduceEvent(state.core, event);
  if (core === state.core) return state;
  const diagnostic = latestDiagnosticChange(state.core, core);
  let next: SessionState = deriveActiveChat({
    ...state,
    core,
    chatConversations: reconcileChatConversations(state.chatConversations, core.chatTranscripts),
  });
  if (diagnostic !== null) next = reportProjectedDiagnostic(next, diagnostic);
  return next;
}

/**
 * Fold a backend checkpoint as one UI transition.
 *
 * Resumed runs can replay failures from an older process before their current
 * ``run_started`` event. Those diagnostics remain in core history, but should
 * not open a stale banner over the newly running session. A batch that ends in
 * failure still reports its terminal diagnostic.
 */
export function applyEventBatch(
  state: SessionState,
  events: readonly RunEvent[],
  activeExecutions?: ActiveExecutionCheckpoint,
  throughSequence?: number,
  historyAfterSequence?: number,
): SessionState {
  return applyReducedCore(
    state,
    reduceEventBatch(state.core, events, activeExecutions, throughSequence, historyAfterSequence),
  );
}

/**
 * Fold a batch that re-bootstraps the stream at a raised history floor, which
 * happens when the run's durable event log is attached after the client
 * subscribed. The batch supersedes the pre-attach state instead of extending
 * it; see `reduceEventRebootstrap`.
 */
export function applyEventRebootstrap(
  state: SessionState,
  events: readonly RunEvent[],
  activeExecutions: ActiveExecutionCheckpoint | undefined,
  throughSequence: number | undefined,
  historyAfterSequence: number,
): SessionState {
  return applyReducedCore(
    state,
    reduceEventRebootstrap(
      state.core,
      events,
      activeExecutions,
      throughSequence,
      historyAfterSequence,
    ),
  );
}

/** The UI transition shared by both ways of folding a backend checkpoint. */
function applyReducedCore(state: SessionState, core: CoreState): SessionState {
  if (core === state.core) return state;
  let next: SessionState = deriveActiveChat({
    ...state,
    core,
    chatConversations: reconcileChatConversations(state.chatConversations, core.chatTranscripts),
  });
  if (core.status === 'failed') {
    const finalDiagnostic = core.diagnostics.at(-1);
    if (finalDiagnostic !== undefined) next = reportProjectedDiagnostic(next, finalDiagnostic);
  }
  return next;
}

/**
 * Fold history older than everything already loaded, lowering the floor below
 * which nothing has been read yet.
 *
 * No diagnostic is projected. A backfilled event is by construction older than
 * every event on screen, so its failure is not news: reporting it would reopen
 * the banner for something the operator has already scrolled past, or already
 * dismissed.
 */
export function applyEventPrefix(
  state: SessionState,
  events: readonly RunEvent[],
  historyAfterSequence: number,
): SessionState {
  const core = reduceEventPrefix(state.core, events, historyAfterSequence);
  if (core === state.core) return state;
  return deriveActiveChat({
    ...state,
    core,
    chatConversations: reconcileChatConversations(state.chatConversations, core.chatTranscripts),
  });
}

/** Folds every thread's replayed transcript into its local conversation. */
function reconcileChatConversations(
  conversations: Record<string, ConversationEntry[]>,
  transcripts: Record<string, TranscriptEntry[]>,
): Record<string, ConversationEntry[]> {
  const next = {...conversations};
  for (const [threadId, transcript] of Object.entries(transcripts)) {
    next[threadId] = reconcileChatTranscript(next[threadId] ?? [], transcript);
  }
  return next;
}

function reconcileChatTranscript(
  conversation: ConversationEntry[],
  transcript: TranscriptEntry[],
): ConversationEntry[] {
  const byId = new Map(transcript.map(entry => [entry.id, entry]));
  const present = new Set<string>();
  const updated = conversation.map(entry => {
    const replacement = byId.get(entry.id);
    if (replacement === undefined) return entry;
    present.add(entry.id);
    return replacement;
  });
  for (const entry of transcript) {
    if (!present.has(entry.id) && !conversation.some(existing => existing.id === entry.id)) {
      updated.push(entry);
    }
  }
  return updated.slice(-500);
}

export function selectNextAgent(state: SessionState): SessionState {
  const phases = visiblePhases(state);
  if (phases.length === 0) return state;
  const current = state.selectedAgentKind;
  const index = current === null ? -1 : phases.findIndex(phase => phase.kind === current);
  const next = phases[(index + 1 + phases.length) % phases.length];
  return {...state, selectedAgentKind: next?.kind ?? null, roundFocus: 'agents', overlay: null};
}

export function selectPreviousAgent(state: SessionState): SessionState {
  const phases = visiblePhases(state);
  if (phases.length === 0) return state;
  const current = state.selectedAgentKind;
  const index = current === null ? 0 : phases.findIndex(phase => phase.kind === current);
  const previous = phases[(index - 1 + phases.length) % phases.length];
  return {
    ...state,
    selectedAgentKind: previous?.kind ?? null,
    roundFocus: 'agents',
    overlay: null,
  };
}

export function selectNextRound(state: SessionState): SessionState {
  const rounds = stripRounds(state);
  if (rounds.length === 0) return state;
  const visible = visibleRoundNumber(state);
  const index = visible === null ? -1 : rounds.findIndex(round => round.number === visible);
  const next = rounds[(index + 1 + rounds.length) % rounds.length];
  return {
    ...state,
    selectedRound: next?.number ?? null,
    selectedAgentKind: null,
    selectedEntryId: null,
    overlay: null,
  };
}

export function selectPreviousRound(state: SessionState): SessionState {
  const rounds = stripRounds(state);
  if (rounds.length === 0) return state;
  const visible = visibleRoundNumber(state);
  const index = visible === null ? 0 : rounds.findIndex(round => round.number === visible);
  const previous = rounds[(index - 1 + rounds.length) % rounds.length];
  return {
    ...state,
    selectedRound: previous?.number ?? null,
    selectedAgentKind: null,
    selectedEntryId: null,
    overlay: null,
  };
}

export function selectRound(state: SessionState, roundNumber: number): SessionState {
  if (!stripRounds(state).some(round => round.number === roundNumber)) return state;
  return {
    ...state,
    selectedRound: roundNumber,
    selectedAgentKind: null,
    selectedEntryId: null,
    overlay: null,
  };
}

export function clearAgentSelection(state: SessionState): SessionState {
  return {
    ...state,
    selectedAgentKind: null,
    selectedEntryId: null,
    roundFocus: 'transcript',
    overlay: null,
  };
}

/**
 * Picks one agent out of the round, or clears the pick when it is already the
 * selected one, so clicking a node twice behaves the way a toggle should. The
 * transcript follows the selection, so the entry cursor starts over with it.
 */
export function selectAgent(state: SessionState, kind: string): SessionState {
  const selected = state.selectedAgentKind === kind ? null : kind;
  return {
    ...state,
    selectedAgentKind: selected,
    selectedEntryId: null,
    roundFocus: 'agents',
    overlay: null,
  };
}

/**
 * Moves the transcript cursor. The cursor is an entry id rather than an index
 * because the visible list changes underneath it as the run streams: an id
 * still points at the same turn after new output arrives.
 */
export function selectNextEntry(state: SessionState, delta: number, id?: string): SessionState {
  const entries = visibleConversation(state);
  if (entries.length === 0) return state;
  // A click names the entry outright; the keys step from wherever the cursor is.
  if (id !== undefined) {
    if (!entries.some(entry => entry.id === id)) return state;
    return {...state, selectedEntryId: state.selectedEntryId === id ? null : id};
  }
  const current =
    state.selectedEntryId === null
      ? -1
      : entries.findIndex(entry => entry.id === state.selectedEntryId);
  // No cursor yet: step in from the end the operator is moving away from.
  const start = current === -1 ? (delta > 0 ? -1 : entries.length) : current;
  const index = Math.min(entries.length - 1, Math.max(0, start + delta));
  return {...state, selectedEntryId: entries[index]?.id ?? null};
}

/**
 * Moves the round view's keys one pane over, in the direction the arrow points.
 * The agent graph is the left pane and the transcript the right one, so this is
 * the spatial move; at either edge it stays put rather than wrapping, because
 * wrapping across the width of the screen is disorienting.
 */
export function focusRound(state: SessionState, focus: RoundFocus): SessionState {
  if (state.roundFocus === focus) return state;
  // Arriving at the graph with nothing picked out puts the cursor on the agent
  // whose turns are on screen, so Tab starts from where the operator is looking.
  if (focus === 'agents' && state.selectedAgentKind === null) {
    const phases = visiblePhases(state);
    const active = phases.find(phase => phase.status === 'active') ?? phases[0];
    return {...state, roundFocus: focus, selectedAgentKind: active?.kind ?? null};
  }
  return {...state, roundFocus: focus};
}

export function clearEntrySelection(state: SessionState): SessionState {
  if (state.selectedEntryId === null) return state;
  return {...state, selectedEntryId: null};
}

/** Moves the todo cursor within the visible phase's list. */
export function selectNextTodo(state: SessionState, delta: number): SessionState {
  const todos = visibleTodos(state);
  if (todos.length === 0) return state;
  const current = state.selectedTodoIndex;
  const start = current === null ? (delta > 0 ? -1 : todos.length) : current;
  const index = Math.min(todos.length - 1, Math.max(0, start + delta));
  return {...state, selectedTodoIndex: index};
}

export interface ErrorReport {
  scope: ErrorScope;
  severity?: ErrorSeverity;
  title?: string;
  diagnostic?: Diagnostic | null;
  diagnosticId?: string | null;
  detail?: string | null;
  hint?: string | null;
  agentKind?: string | null;
  roundLabel?: string | null;
  invocationId?: string | null;
}

/** Acknowledges the visible diagnostic without changing any run or view state. */
export function dismissErrorBanner(state: SessionState): SessionState {
  if (state.errorBanner === null) return state;
  return {...state, errorBanner: null};
}

/**
 * Records an error independently of any particular view. A terminal event
 * commonly repeats an invocation failure, so equivalent reports promote the
 * current banner instead of burying its cause beneath a duplicate.
 */
export function reportError(
  state: SessionState,
  message: string,
  report: ErrorReport,
): SessionState {
  const diagnostic = report.diagnostic ?? null;
  const scope = diagnostic?.scope ?? report.scope;
  const severity = diagnosticSeverity(diagnostic?.severity) ?? report.severity ?? 'recoverable';
  const banner: ErrorBannerState = {
    title: report.title ?? errorTitle(scope),
    message: diagnostic?.summary || message || 'An unknown error occurred.',
    detail: diagnostic?.detail ?? report.detail ?? null,
    hint: diagnostic?.hint ?? report.hint ?? null,
    diagnosticId: diagnostic?.id ?? report.diagnosticId ?? null,
    severity,
    scope,
    agentKind: report.agentKind ?? null,
    roundLabel: report.roundLabel ?? null,
    invocationId: report.invocationId ?? null,
    count: 1,
  };
  const existing = state.errorBanner;
  if (existing === null || !equivalentError(existing, banner)) {
    return {...state, errorBanner: banner};
  }
  const promoted = existing.severity === 'fatal' || severity === 'fatal' ? 'fatal' : 'recoverable';
  return {
    ...state,
    errorBanner: {
      ...existing,
      message: moreInformativeMessage(existing.message, banner.message),
      detail: moreInformativeMessage(existing.detail ?? '', banner.detail ?? '') || null,
      hint: moreInformativeMessage(existing.hint ?? '', banner.hint ?? '') || null,
      severity: promoted,
      title: promoted === 'fatal' ? banner.title : existing.title,
      scope: promoted === 'fatal' ? banner.scope : existing.scope,
      diagnosticId: existing.diagnosticId ?? banner.diagnosticId,
      agentKind: existing.agentKind ?? banner.agentKind,
      roundLabel: existing.roundLabel ?? banner.roundLabel,
      invocationId: existing.invocationId ?? banner.invocationId,
      count: existing.count + 1,
    },
  };
}

function reportProjectedDiagnostic(state: SessionState, diagnostic: CoreDiagnostic): SessionState {
  return reportError(state, diagnostic.summary, {
    scope: diagnostic.scope,
    severity: diagnostic.severity === 'fatal' ? 'fatal' : 'recoverable',
    title: projectedDiagnosticTitle(diagnostic.failureKind),
    diagnosticId: diagnostic.id,
    detail: diagnostic.detail,
    hint: diagnostic.hint,
    agentKind: diagnostic.agentKind,
    roundLabel: diagnostic.roundLabel,
    invocationId: diagnostic.invocationId,
  });
}

function projectedDiagnosticTitle(kind: CoreDiagnostic['failureKind']): string {
  if (kind === 'run_interruption') return 'Run interrupted';
  return errorTitle(kind);
}

/** Keep a terminal wrapper's extra context when it repeats an invocation error. */
function moreInformativeMessage(current: string, later: string): string {
  if (later.length >= current.length) return later;
  const currentLines = current.split('\n').length;
  const laterLines = later.split('\n').length;
  return laterLines > currentLines ? later : current;
}

function equivalentError(left: ErrorBannerState, right: ErrorBannerState): boolean {
  if (left.diagnosticId !== null && left.diagnosticId === right.diagnosticId) return true;
  if (left.invocationId !== null && left.invocationId === right.invocationId) return true;
  const leftMessage = left.message.trim();
  const rightMessage = right.message.trim();
  if (leftMessage === rightMessage) return true;
  // Terminal wrappers often add only an exception prefix around an invocation
  // error. Fold those together, but do not equate short generic diagnostics.
  return (
    Math.min(leftMessage.length, rightMessage.length) >= 24 &&
    (leftMessage.includes(rightMessage) || rightMessage.includes(leftMessage))
  );
}

function errorTitle(scope: ErrorScope): string {
  const titles: Record<ErrorScope, string> = {
    configuration: 'Configuration failed',
    invocation: 'Invocation failed',
    phase: 'Phase failed',
    run: 'Run failed',
    protocol: 'Protocol error',
    request: 'Request failed',
    transport: 'Connection lost',
    input: 'Input error',
  };
  return titles[scope];
}

function diagnosticSeverity(
  severity: Diagnostic['severity'] | undefined,
): ErrorSeverity | undefined {
  if (severity === undefined) return undefined;
  return severity === 'fatal' ? 'fatal' : 'recoverable';
}

/**
 * Returns to the experiment log. Per-round output is reachable only by opening
 * a hypothesis, so there is no unfiltered live view to fall back to and the
 * log is never dismissed.
 */
export function showLive(state: SessionState): SessionState {
  return {
    ...state,
    overlay: null,
    chatOpen: false,
    hypothesisDetail: null,
    hypothesisScope: null,
    selectedRound: null,
    selectedAgentKind: null,
    experimentLog: state.experimentLog ?? {
      entries: [],
      selectedId: null,
      pending: true,
      error: null,
    },
  };
}

export function showDetail(
  state: SessionState,
  content: string,
  kind: OverlayPanel['kind'] = 'detail',
): SessionState {
  return {...state, overlay: {kind, content}};
}

/**
 * How one backend run status reads in the header.
 *
 * The header shows exactly one status token, and it is this one: the backend
 * owns the run's lifecycle, so there is no second flag to prefix. The switch is
 * exhaustive, making a new status a compile error rather than a raw token.
 */
export function runStatusLabel(status: CoreRunStatus): string {
  switch (status) {
    case 'pausing':
      // Requested, but the call already in flight has to finish first.
      return 'pausing…';
    case 'connecting':
    case 'starting':
    case 'running':
    case 'paused':
    case 'completed':
    case 'failed':
      return status;
    default: {
      const unhandled: never = status;
      return unhandled;
    }
  }
}

export function visibleConversation(state: SessionState): ConversationEntry[] {
  const roundNumber = visibleRoundNumber(state);
  return state.core.transcript.filter(entry => {
    if (roundNumber !== null && entry.roundNumber !== roundNumber) return false;
    if (state.selectedAgentKind !== null && entry.agentKind !== state.selectedAgentKind) {
      return false;
    }
    return true;
  });
}

export function visiblePhases(state: SessionState): AgentPhase[] {
  return phasesForRound(state.core.phases, visibleRoundNumber(state));
}

export function toggleTodos(state: SessionState): SessionState {
  return {...state, todosExpanded: !state.todosExpanded};
}

/**
 * The todo list for the execution the operator is looking at, following the same
 * scoping rules as the conversation filter. Entries whose events carried no
 * agent or round stamp (legacy streams) match any scope rather than vanish.
 */
export function visibleTodos(state: SessionState): TodoItem[] {
  const roundNumber = visibleRoundNumber(state);
  const matchesRound = (phase: ExecutionTodos): boolean =>
    roundNumber === null || phase.roundNumber === roundNumber || phase.roundNumber === null;
  const latestFirst = [...state.core.todos].reverse();
  if (state.selectedAgentKind !== null) {
    const selected = state.selectedAgentKind;
    return (
      latestFirst.find(
        phase => (phase.agentKind === selected || phase.agentKind === null) && matchesRound(phase),
      )?.items ?? []
    );
  }
  if (state.selectedRound !== null) {
    // Browsing a round without an agent selected: show the round's most
    // recently updated list, i.e. its final todo state.
    return latestFirst.find(matchesRound)?.items ?? [];
  }
  // Live view: follow the currently active agent so a phase that never
  // emits todos shows nothing instead of the previous phase's leftovers.
  return (
    latestFirst.find(
      phase =>
        (phase.agentKind === state.core.agentKind || phase.agentKind === null) &&
        matchesRound(phase),
    )?.items ?? []
  );
}

export function visibleRoundNumber(state: SessionState): number | null {
  const scope = state.hypothesisScope;
  if (scope !== null && state.selectedRound === null) {
    // Opening a hypothesis lands on one of its rounds rather than on nothing.
    // Leaving the round unset used to mean "the whole trajectory", which drew an
    // empty agent strip and an empty transcript for any run whose events are not
    // all round-stamped: a blank view where a round was asked for.
    const rounds = state.core.rounds.filter(round => scope.rounds.includes(round.number));
    const active = [...rounds].reverse().find(round => round.status === 'active');
    return active?.number ?? rounds.at(-1)?.number ?? scope.rounds.at(-1) ?? null;
  }
  if (state.selectedRound !== null) return state.selectedRound;
  const rounds = stripRounds(state);
  const active = [...rounds].reverse().find(round => round.status === 'active');
  return active?.number ?? rounds.at(-1)?.number ?? null;
}

/** The rounds owned by the hypothesis on screen, or every round outside one. */
export function scopedRounds(state: SessionState): RoundState[] {
  const scope = state.hypothesisScope;
  if (scope === null) return state.core.rounds;
  return state.core.rounds.filter(round => scope.rounds.includes(round.number));
}

/**
 * What the strip shows and what round navigation moves through: every round of
 * the run, including ones it has not reached yet. A run announces how many
 * rounds it intends to take, so the strip can show the shape of the whole run
 * from the first round rather than growing one chip at a time. Rounds beyond
 * what has happened carry ``planned`` and open an empty view, which is the
 * honest thing to show for a round that has not run.
 */
export function stripRounds(state: SessionState): RoundState[] {
  const highest = Math.max(
    state.core.maxRounds ?? 0,
    ...state.core.rounds.map(round => round.number),
    0,
  );
  if (highest === 0) return state.core.rounds;
  const known = new Map(state.core.rounds.map(round => [round.number, round]));
  const rounds: RoundState[] = [];
  for (let number = 1; number <= highest; number += 1) {
    rounds.push(known.get(number) ?? {number, status: 'planned'});
  }
  return rounds;
}
