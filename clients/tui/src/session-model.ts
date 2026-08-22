import type {HypothesisEntry, RunEvent, RunSnapshot} from './protocol.js';
import {
  type AgentPhase,
  applyRunMapEvent,
  type RoundSummary,
  roundNumberFromLabel,
  visiblePhases as visibleRunMapPhases,
  visibleRoundNumber as visibleRunMapRoundNumber,
} from './run-map.js';
import {DEFAULT_THEME_NAME, THEME_NAMES, type ThemeName} from './ui/theme.js';

export interface SessionState {
  sequence: number;
  status: string;
  agentKind: string | null;
  roundLabel: string | null;
  outerLoop: string | null;
  rounds: RoundSummary[];
  phases: AgentPhase[];
  selectedRound: number | null;
  selectedAgentKind: string | null;
  conversation: ConversationEntry[];
  overlay: OverlayPanel | null;
  chatOpen: boolean;
  chatConversation: ConversationEntry[];
  chatPending: boolean;
  chatTypedToolEvents: boolean;
  terminal: boolean;
  todoPhases: PhaseTodos[];
  todosExpanded: boolean;
  usage: UsageMeter | null;
  themeName: ThemeName;
  experimentLog: ExperimentLogState | null;
  hypothesisScope: HypothesisScope | null;
  layout: LayoutState;
  /** Non-null while the theme list is open as a keyboard selection. */
  themePicker: ThemePicker | null;
  /**
   * Set once a typed tool_call/tool_result event is seen. From then on the
   * legacy tool-channel text chunks (still present in event files recorded
   * by older backends) are ignored so tool turns never render twice.
   */
  typedToolEvents: boolean;
}

export interface TodoItem {
  content: string;
  status: string;
}

/**
 * One agent phase's latest todo-list snapshot. Todos are keyed per
 * (round, agent) so concurrent or successive phases never clobber each
 * other's lists, mirroring how the conversation filter scopes entries.
 */
export interface PhaseTodos {
  agentKind: string | null;
  roundNumber: number | null;
  items: TodoItem[];
}

/**
 * The experiment log is open when this is non-null. Selection is held as a
 * hypothesis id rather than a row index so a refresh that inserts rows keeps
 * the operator on the same hypothesis.
 */
export interface ExperimentLogState {
  entries: HypothesisEntry[];
  selectedId: string | null;
  pending: boolean;
  error: string | null;
}

/**
 * The hypothesis whose rounds the operator opened. While this is set the
 * client shows the ordinary per-round trajectory, filtered to these rounds,
 * and the log table steps aside without losing its selection.
 */
export interface HypothesisScope {
  id: string;
  label: string;
  rounds: number[];
}

/**
 * A visualization command's output, rendered beside the transcript rather than
 * over it. Content is pre-rendered text so the pane stays agnostic about which
 * command produced it and a new command needs no new layout code.
 */
export type PaneView = 'perf';

export interface RightPane {
  view: PaneView;
  title: string;
  content: string;
  pending: boolean;
  error: string | null;
}

export interface LayoutState {
  /** null means single-pane: the transcript has the full width. */
  right: RightPane | null;
  focus: 'left' | 'right';
}

export interface ThemePicker {
  selected: ThemeName;
}

export interface UsageMeter {
  inputTokens: number;
  contextWindow: number | null;
  model: string | null;
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
}

export function initialSessionState(themeName: ThemeName = DEFAULT_THEME_NAME): SessionState {
  return {
    sequence: 0,
    status: 'connecting',
    agentKind: null,
    roundLabel: null,
    outerLoop: null,
    rounds: [],
    phases: [],
    selectedRound: null,
    selectedAgentKind: null,
    conversation: [],
    overlay: null,
    chatOpen: false,
    chatConversation: [],
    chatPending: false,
    chatTypedToolEvents: false,
    terminal: false,
    todoPhases: [],
    todosExpanded: false,
    usage: null,
    themeName,
    // The experiment log is the landing view: a run's history reads as a short
    // list of claims before it reads as a long list of rounds.
    experimentLog: {entries: [], selectedId: null, pending: true, error: null},
    hypothesisScope: null,
    layout: {right: null, focus: 'left'},
    themePicker: null,
    typedToolEvents: false,
  };
}

/**
 * Rows are keyed by hypothesis id, which the server orders by first round and
 * never reshuffles. Selection therefore survives a refresh even when a new
 * hypothesis lands above the current one.
 */
export function entryKey(entry: HypothesisEntry, index: number): string {
  return entry.identified === false
    ? `${entry.hypothesis_id}#${entry.first_round}`
    : entry.hypothesis_id || `#${index}`;
}

/** True when the table itself is on screen rather than a hypothesis trajectory. */
export function experimentLogVisible(state: SessionState): boolean {
  return state.experimentLog !== null && state.hypothesisScope === null && !state.chatOpen;
}

export function openExperimentLog(state: SessionState): SessionState {
  const existing = state.experimentLog;
  return {
    ...state,
    overlay: null,
    chatOpen: false,
    hypothesisScope: null,
    selectedRound: null,
    selectedAgentKind: null,
    experimentLog: existing ?? {entries: [], selectedId: null, pending: true, error: null},
  };
}

export function setExperiments(state: SessionState, entries: HypothesisEntry[]): SessionState {
  const log = state.experimentLog;
  if (log === null) return state;
  const keys = entries.map(entryKey);
  // Keep the operator's row when it still exists; otherwise fall back to the
  // active hypothesis, then to the first row.
  const selectedId =
    log.selectedId !== null && keys.includes(log.selectedId)
      ? log.selectedId
      : (keys[entries.findIndex(entry => entry.active === true)] ?? keys[0] ?? null);
  return {
    ...state,
    experimentLog: {...log, entries, selectedId, pending: false, error: null},
  };
}

export function failExperiments(state: SessionState, error: string): SessionState {
  const log = state.experimentLog;
  if (log === null) return state;
  return {...state, experimentLog: {...log, pending: false, error}};
}

export function moveExperimentSelection(state: SessionState, delta: number): SessionState {
  const log = state.experimentLog;
  if (log === null || state.hypothesisScope !== null || log.entries.length === 0) return state;
  const keys = log.entries.map(entryKey);
  const current = log.selectedId === null ? 0 : keys.indexOf(log.selectedId);
  const index = Math.min(keys.length - 1, Math.max(0, (current === -1 ? 0 : current) + delta));
  return {...state, experimentLog: {...log, selectedId: keys[index] ?? null}};
}

/**
 * Opens the rounds behind the selected hypothesis. The log keeps its selection
 * so leaving the trajectory lands the operator back on the same row.
 */
export function enterExperimentDrilldown(state: SessionState): SessionState {
  const entry = selectedExperiment(state);
  if (entry === null || state.hypothesisScope !== null) return state;
  const rounds = scopeRounds(entry);
  if (rounds.length === 0) return state;
  return {
    ...state,
    overlay: null,
    hypothesisScope: {id: entry.hypothesis_id, label: hypothesisLabel(entry), rounds},
    selectedRound: null,
    selectedAgentKind: null,
  };
}

/** Leaves the trajectory and returns to the table with the selection intact. */
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
  const entry = (state.experimentLog?.entries ?? []).find(candidate =>
    scopeRounds(candidate).includes(roundNumber),
  );
  if (entry === undefined) return null;
  const scoped: SessionState = {
    ...state,
    overlay: null,
    chatOpen: false,
    hypothesisScope: {
      id: entry.hypothesis_id,
      label: hypothesisLabel(entry),
      rounds: scopeRounds(entry),
    },
    selectedRound: roundNumber,
    selectedAgentKind: null,
    experimentLog:
      state.experimentLog === null
        ? null
        : {...state.experimentLog, selectedId: entryKeyFor(state.experimentLog.entries, entry)},
  };
  return scoped;
}

function entryKeyFor(entries: HypothesisEntry[], entry: HypothesisEntry): string | null {
  const index = entries.indexOf(entry);
  return index === -1 ? null : entryKey(entry, index);
}

function scopeRounds(entry: HypothesisEntry): number[] {
  const listed = (entry.rounds ?? []).map(round => round.round);
  if (listed.length > 0) return [...listed].sort((a, b) => a - b);
  // An active hypothesis whose first round has not finished has no round
  // records yet, but its opening round is still worth showing.
  return entry.first_round > 0 ? [entry.first_round] : [];
}

function hypothesisLabel(entry: HypothesisEntry): string {
  const range =
    entry.first_round === entry.last_round
      ? `r${entry.first_round}`
      : `r${entry.first_round}-${entry.last_round}`;
  return `${entry.hypothesis_id} · ${range}`;
}

export function selectedExperiment(state: SessionState): HypothesisEntry | null {
  const log = state.experimentLog;
  if (log === null || log.selectedId === null) return null;
  const index = log.entries.map(entryKey).indexOf(log.selectedId);
  return index === -1 ? null : (log.entries[index] ?? null);
}

export const PANE_TITLES: Record<PaneView, string> = {
  perf: 'Performance',
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
  return {...state, layout: {right: null, focus: 'left'}};
}

export function togglePaneFocus(state: SessionState): SessionState {
  if (state.layout.right === null) return state;
  return {
    ...state,
    layout: {...state.layout, focus: state.layout.focus === 'left' ? 'right' : 'left'},
  };
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
  return {
    ...state,
    status: snapshot.status,
    agentKind: snapshot.agent_kind ?? null,
    roundLabel: snapshot.round_label ?? null,
    terminal: snapshot.status === 'completed' || snapshot.status === 'failed',
  };
}

export function applyEvent(state: SessionState, event: RunEvent): SessionState {
  const sequence = event.sequence ?? 0;
  if (sequence > 0 && sequence <= state.sequence) return state;
  const next = {...state, sequence: Math.max(state.sequence, sequence)};
  if (event.agent_kind === 'chat') return applyChatEvent(next, event);
  if (event.agent_kind) next.agentKind = event.agent_kind;
  if (event.round_label) next.roundLabel = event.round_label;
  const runMap = applyRunMapEvent(
    {outerLoop: next.outerLoop, rounds: next.rounds, phases: next.phases},
    event,
  );
  next.outerLoop = runMap.outerLoop;
  next.rounds = runMap.rounds;
  next.phases = runMap.phases;

  const data = event.data;
  if (data?.kind === 'tool_call' || data?.kind === 'tool_result') {
    next.typedToolEvents = true;
  }
  if (data?.kind === 'todo_update') {
    const items = (data.todos ?? []).map(todo => ({
      content: String(todo.content),
      status: String(todo.status),
    }));
    const agentKind = event.agent_kind ?? null;
    const roundNumber = roundNumberFromLabel(event.round_label);
    next.todoPhases = [
      ...next.todoPhases.filter(
        phase => phase.agentKind !== agentKind || phase.roundNumber !== roundNumber,
      ),
      {agentKind, roundNumber, items},
    ].slice(-100);
  }
  if (data?.kind === 'usage_update') {
    next.usage = {
      inputTokens: data.input_tokens,
      contextWindow: data.context_window ?? null,
      model: data.model ?? null,
    };
  }

  // Prefer typed tool events; fall back to legacy tool-channel chunks only
  // for streams that never produce typed events (old event files / replays).
  const suppressed =
    data?.kind === 'agent_output_chunk' && data.channel === 'tool' && next.typedToolEvents;
  if (!suppressed) {
    const entry = eventToConversationEntry(event);
    if (entry !== null) next.conversation = appendConversation(next.conversation, entry);
  }

  if (event.type === 'run_started') {
    next.status = 'running';
  }
  if (event.type === 'configuration_failed') {
    next.status = 'failed';
    next.terminal = true;
  }
  if (event.type === 'run_finished') {
    next.status = 'completed';
    next.terminal = true;
  }
  if (event.type === 'run_failed' || event.type === 'run_interrupted') {
    next.status = 'failed';
    next.terminal = true;
  }
  return next;
}

function applyChatEvent(state: SessionState, event: RunEvent): SessionState {
  const next = {...state};
  const data = event.data;
  if (data?.kind === 'tool_call' || data?.kind === 'tool_result') {
    next.chatTypedToolEvents = true;
  }
  const suppressed =
    data?.kind === 'agent_output_chunk' && data.channel === 'tool' && next.chatTypedToolEvents;
  if (suppressed) return next;
  const entry = eventToConversationEntry(event);
  if (entry !== null) {
    next.chatConversation = appendConversation(next.chatConversation, entry);
  }
  return next;
}

export function selectNextAgent(state: SessionState): SessionState {
  const phases = visiblePhases(state);
  if (phases.length === 0) return state;
  const current = state.selectedAgentKind;
  const index = current === null ? -1 : phases.findIndex(phase => phase.kind === current);
  const next = phases[(index + 1 + phases.length) % phases.length];
  return {...state, selectedAgentKind: next?.kind ?? null, overlay: null};
}

export function selectPreviousAgent(state: SessionState): SessionState {
  const phases = visiblePhases(state);
  if (phases.length === 0) return state;
  const current = state.selectedAgentKind;
  const index = current === null ? 0 : phases.findIndex(phase => phase.kind === current);
  const previous = phases[(index - 1 + phases.length) % phases.length];
  return {...state, selectedAgentKind: previous?.kind ?? null, overlay: null};
}

export function selectNextRound(state: SessionState): SessionState {
  const rounds = scopedRounds(state);
  if (rounds.length === 0) return state;
  const visible = visibleRoundNumber(state);
  const index = visible === null ? -1 : rounds.findIndex(round => round.number === visible);
  const next = rounds[(index + 1 + rounds.length) % rounds.length];
  return {...state, selectedRound: next?.number ?? null, selectedAgentKind: null, overlay: null};
}

export function selectPreviousRound(state: SessionState): SessionState {
  const rounds = scopedRounds(state);
  if (rounds.length === 0) return state;
  const visible = visibleRoundNumber(state);
  const index = visible === null ? 0 : rounds.findIndex(round => round.number === visible);
  const previous = rounds[(index - 1 + rounds.length) % rounds.length];
  return {
    ...state,
    selectedRound: previous?.number ?? null,
    selectedAgentKind: null,
    overlay: null,
  };
}

export function selectRound(state: SessionState, roundNumber: number): SessionState {
  if (!scopedRounds(state).some(round => round.number === roundNumber)) return state;
  return {...state, selectedRound: roundNumber, selectedAgentKind: null, overlay: null};
}

export function selectAgent(state: SessionState, kind: string): SessionState {
  return {...state, selectedAgentKind: kind, overlay: null};
}

export function clearAgentSelection(state: SessionState): SessionState {
  return {...state, selectedAgentKind: null, overlay: null};
}

function formatConfigurationFailure(event: RunEvent): string {
  const data = event.data;
  if (data?.kind !== 'configuration_failed') return event.text || 'Configuration failed.';
  const sections = [data.message];
  if (data.usage) sections.push(data.usage);
  sections.push(`Code: ${data.code} · Stage: ${data.stage}`);
  return sections.join('\n\n');
}

function eventToConversationEntry(event: RunEvent): ConversationEntry | null {
  const data = event.data;
  const id = String(event.sequence ?? `${event.timestamp}-${event.type}`);
  const agentKind = event.agent_kind ? {agentKind: event.agent_kind} : {};
  const roundLabel = event.round_label ? {roundLabel: event.round_label} : {};
  const roundNumber = roundNumberFromLabel(event.round_label);
  const roundFields = {
    ...roundLabel,
    ...(roundNumber === null ? {} : {roundNumber}),
  };
  if (data?.kind === 'configuration_failed') {
    return {
      id,
      kind: 'result',
      content: formatConfigurationFailure(event),
      label: 'Configuration failed',
      tone: 'failure',
    };
  }
  if (data?.kind === 'chat') {
    return {
      id,
      kind: 'assistant',
      content: data.answer,
      label: 'Answer',
      ...agentKind,
      ...roundFields,
    };
  }
  if (data?.kind === 'agent_output_chunk') {
    const kind =
      data.channel === 'assistant'
        ? 'assistant'
        : data.channel === 'prompt'
          ? 'prompt'
          : data.channel === 'analysis'
            ? 'analysis'
            : data.channel === 'tool'
              ? 'tool'
              : 'diagnostic';
    const invocationId = event.invocation_id ?? undefined;
    return {
      id,
      kind,
      content: data.content,
      label: labelFor(event, data.channel),
      ...agentKind,
      ...roundFields,
      turnId: invocationId ?? id,
      ...(invocationId === undefined ? {} : {invocationId}),
      startsTurn:
        kind === 'tool' && data.channel === 'tool' && data.content.trimStart().startsWith('→ '),
      ...(kind === 'tool' && data.channel === 'tool' && data.content.trimStart().startsWith('→ ')
        ? {toolCall: data.content}
        : {}),
    };
  }
  if (data?.kind === 'tool_call') {
    const call = formatToolCall(data.tool, data.args ?? {});
    const invocationId = event.invocation_id ?? undefined;
    return {
      id,
      kind: 'tool',
      content: call,
      label: labelFor(event, 'tool'),
      ...agentKind,
      ...roundFields,
      turnId: invocationId ?? id,
      ...(invocationId === undefined ? {} : {invocationId}),
      startsTurn: true,
      toolCall: call,
      toolName: data.tool,
      ...(data.call_id === null || data.call_id === undefined ? {} : {toolCallId: data.call_id}),
    };
  }
  if (data?.kind === 'tool_result') {
    const invocationId = event.invocation_id ?? undefined;
    return {
      id,
      kind: 'tool',
      content: data.content,
      label: labelFor(event, 'tool'),
      ...(data.is_error ? {tone: 'failure' as const} : {}),
      ...agentKind,
      ...roundFields,
      turnId: invocationId ?? id,
      toolName: data.tool,
      ...(data.call_id === null || data.call_id === undefined ? {} : {toolCallId: data.call_id}),
      ...(invocationId === undefined ? {} : {invocationId}),
    };
  }
  if (data?.kind === 'subprocess_output') {
    return {
      id,
      kind: 'subprocess',
      content: data.content,
      label: `${data.process_kind} · ${data.stream}`,
      ...agentKind,
      ...roundFields,
    };
  }
  if (event.type === 'phase_started') {
    return {
      id,
      kind: 'status',
      content: 'started',
      label: labelFor(event, 'phase'),
      ...agentKind,
      ...roundFields,
    };
  }
  if (data?.kind === 'judge_result') {
    return {
      id,
      kind: 'result',
      content: data.feedback || `Judge returned ${data.verdict}.`,
      label: `Judge · ${data.verdict.toUpperCase()}`,
      tone: data.verdict === 'pass' ? 'success' : 'failure',
      ...agentKind,
      ...roundFields,
    };
  }
  if (data?.kind === 'benchmark_result') {
    return {
      id,
      kind: 'result',
      content: `${data.metric}: ${data.value} ${data.unit}`,
      label: 'Benchmark',
      tone: 'success',
      ...agentKind,
      ...roundFields,
    };
  }
  if (data?.kind === 'round_finished') {
    const tone =
      data.judge_verdict === 'pass'
        ? ('success' as const)
        : data.judge_verdict === 'fail'
          ? ('failure' as const)
          : ('normal' as const);
    return {
      id,
      kind: 'result',
      content: `${data.attempts} attempt(s)`,
      label: `${event.round_label ?? 'Round'} · ${data.judge_verdict.toUpperCase()}`,
      tone,
      ...agentKind,
      ...roundFields,
    };
  }
  if (event.type === 'run_failed' || event.type === 'run_interrupted') {
    return {
      id,
      kind: 'result',
      content: event.text || 'Run interrupted.',
      label: 'Run failed',
      tone: 'failure',
      ...agentKind,
      ...roundFields,
    };
  }
  return null;
}

function labelFor(event: RunEvent, fallback: string): string {
  const phase = event.agent_kind ?? fallback;
  return event.round_label ? `${phase} · ${event.round_label}` : phase;
}

function appendConversation(
  previous: ConversationEntry[],
  incoming: ConversationEntry,
): ConversationEntry[] {
  if (incoming.kind === 'tool' && !incoming.startsTurn && incoming.toolName !== undefined) {
    const target = findToolCall(previous, incoming);
    if (target !== -1) {
      return previous.map((entry, index) =>
        index === target ? mergeToolResult(entry, incoming) : entry,
      );
    }
  }
  const last = previous.at(-1);
  if (
    last &&
    incoming.kind === 'tool' &&
    last.kind === 'tool' &&
    last.invocationId === incoming.invocationId &&
    !incoming.startsTurn
  ) {
    return [...previous.slice(0, -1), mergeToolResult(last, incoming)];
  }
  if (
    last &&
    last.kind === incoming.kind &&
    last.turnId === incoming.turnId &&
    (incoming.kind === 'assistant' || incoming.kind === 'prompt' || incoming.kind === 'analysis')
  ) {
    return [...previous.slice(0, -1), {...last, content: last.content + incoming.content}];
  }
  return [...previous, incoming].slice(-1_000);
}

function findToolCall(previous: ConversationEntry[], result: ConversationEntry): number {
  const indices = Array.from(previous.keys());
  if (result.toolCallId !== undefined) indices.reverse();
  for (const index of indices) {
    const candidate = previous[index];
    if (
      candidate?.kind !== 'tool' ||
      candidate.toolCall === undefined ||
      candidate.toolResponse !== undefined ||
      candidate.invocationId !== result.invocationId
    ) {
      continue;
    }
    if (result.toolCallId !== undefined) {
      if (candidate.toolCallId === result.toolCallId) return index;
      continue;
    }
    if (candidate.toolName === result.toolName) return index;
  }
  return -1;
}

function mergeToolResult(call: ConversationEntry, result: ConversationEntry): ConversationEntry {
  const separator = call.content.endsWith('\n') || result.content.startsWith('\n') ? '' : '\n';
  return {
    ...call,
    content: call.content + separator + result.content,
    toolResponse: (call.toolResponse ?? '') + (call.toolResponse ? separator : '') + result.content,
    ...(result.tone === undefined ? {} : {tone: result.tone}),
  };
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

export function statusText(state: SessionState): string {
  const base = `${state.status} · ${state.agentKind ?? 'starting'} · ${state.roundLabel ?? 'no round yet'}`;
  if (state.usage === null) return base;
  const used = formatTokenCount(state.usage.inputTokens);
  const meter =
    state.usage.contextWindow === null
      ? used
      : `${used}/${formatTokenCount(state.usage.contextWindow)}`;
  return `${base} · ${meter} tokens`;
}

function formatTokenCount(count: number): string {
  if (count < 1_000) return String(count);
  if (count < 1_000_000) return `${Math.floor(count / 1_000)}k`;
  return `${(count / 1_000_000).toFixed(1)}M`;
}

export function visibleConversation(state: SessionState): ConversationEntry[] {
  const scope = state.hypothesisScope;
  // Inside a hypothesis with no round picked out, show the whole trajectory of
  // that hypothesis rather than only its latest round.
  if (scope !== null && state.selectedRound === null) {
    return state.conversation.filter(entry => {
      if (entry.roundNumber === undefined || !scope.rounds.includes(entry.roundNumber)) {
        return false;
      }
      return state.selectedAgentKind === null || entry.agentKind === state.selectedAgentKind;
    });
  }
  const roundNumber = visibleRoundNumber(state);
  return state.conversation.filter(entry => {
    if (roundNumber !== null && entry.roundNumber !== roundNumber) return false;
    if (state.selectedAgentKind !== null && entry.agentKind !== state.selectedAgentKind) {
      return false;
    }
    return true;
  });
}

export function visiblePhases(state: SessionState): AgentPhase[] {
  return visibleRunMapPhases(state.phases, visibleRoundNumber(state));
}

export function toggleTodos(state: SessionState): SessionState {
  return {...state, todosExpanded: !state.todosExpanded};
}

/**
 * The todo list for the phase the operator is looking at, following the same
 * scoping rules as the conversation filter. Entries whose events carried no
 * agent or round stamp (legacy streams) match any scope rather than vanish.
 */
export function visibleTodos(state: SessionState): TodoItem[] {
  const roundNumber = visibleRoundNumber(state);
  const matchesRound = (phase: PhaseTodos): boolean =>
    roundNumber === null || phase.roundNumber === roundNumber || phase.roundNumber === null;
  const latestFirst = [...state.todoPhases].reverse();
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
        (phase.agentKind === state.agentKind || phase.agentKind === null) && matchesRound(phase),
    )?.items ?? []
  );
}

export function visibleRoundNumber(state: SessionState): number | null {
  if (state.hypothesisScope !== null && state.selectedRound === null) return null;
  return visibleRunMapRoundNumber(scopedRounds(state), state.selectedRound);
}

/** The rounds the strip and round navigation operate on for the current view. */
export function scopedRounds(state: SessionState): RoundSummary[] {
  const scope = state.hypothesisScope;
  if (scope === null) return state.rounds;
  return state.rounds.filter(round => scope.rounds.includes(round.number));
}

const MAX_TOOL_ARG_LEN = 80;

function formatToolCall(tool: string, args: Record<string, unknown>): string {
  const parts = Object.entries(args).map(([key, value]) => {
    const isString = typeof value === 'string';
    let rendered = isString ? value : (JSON.stringify(value) ?? String(value));
    if (rendered.length > MAX_TOOL_ARG_LEN) {
      rendered = `${rendered.slice(0, MAX_TOOL_ARG_LEN)}...`;
    }
    return isString ? `${key}="${rendered}"` : `${key}=${rendered}`;
  });
  return `→ ${tool}(${parts.join(', ')})\n`;
}
