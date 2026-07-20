import type {RunEvent, RunSnapshot} from './protocol.js';
import {
  type AgentPhase,
  applyRunMapEvent,
  type RoundSummary,
  roundNumberFromLabel,
  visiblePhases as visibleRunMapPhases,
  visibleRoundNumber as visibleRunMapRoundNumber,
} from './run-map.js';

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
  terminal: boolean;
  todoPhases: PhaseTodos[];
  todosExpanded: boolean;
  usage: UsageMeter | null;
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

export function initialSessionState(): SessionState {
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
    terminal: false,
    todoPhases: [],
    todosExpanded: false,
    usage: null,
    typedToolEvents: false,
  };
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
  if (state.rounds.length === 0) return state;
  const visible = visibleRoundNumber(state);
  const index = visible === null ? -1 : state.rounds.findIndex(round => round.number === visible);
  const next = state.rounds[(index + 1 + state.rounds.length) % state.rounds.length];
  return {...state, selectedRound: next?.number ?? null, selectedAgentKind: null, overlay: null};
}

export function selectPreviousRound(state: SessionState): SessionState {
  if (state.rounds.length === 0) return state;
  const visible = visibleRoundNumber(state);
  const index = visible === null ? 0 : state.rounds.findIndex(round => round.number === visible);
  const previous = state.rounds[(index - 1 + state.rounds.length) % state.rounds.length];
  return {
    ...state,
    selectedRound: previous?.number ?? null,
    selectedAgentKind: null,
    overlay: null,
  };
}

export function selectRound(state: SessionState, roundNumber: number): SessionState {
  if (!state.rounds.some(round => round.number === roundNumber)) return state;
  return {...state, selectedRound: roundNumber, selectedAgentKind: null, overlay: null};
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
    return {
      id,
      kind: 'result',
      content: `${data.attempts} attempt(s)`,
      label: `${event.round_label ?? 'Round'} · ${data.judge_verdict.toUpperCase()}`,
      tone: data.judge_verdict === 'pass' ? 'success' : 'failure',
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

export function showLive(state: SessionState): SessionState {
  return {...state, overlay: null, selectedRound: null, selectedAgentKind: null};
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
  return visibleRunMapRoundNumber(state.rounds, state.selectedRound);
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
