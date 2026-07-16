import type {RunEvent, RunSnapshot} from './protocol.js';
import {
  applyRunMapEvent,
  roundNumberFromLabel,
  visiblePhases as visibleRunMapPhases,
  visibleRoundNumber as visibleRunMapRoundNumber,
  type AgentPhase,
  type RoundSummary,
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
  liveContent: string;
  conversation: ConversationEntry[];
  overlay: OverlayPanel | null;
  terminal: boolean;
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
    liveContent: 'Waiting for run events…',
    conversation: [],
    overlay: null,
    terminal: false,
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

  const line = renderEventForTranscript(event);
  if (line !== null) next.liveContent = appendTranscript(next.liveContent, line);
  const entry = eventToConversationEntry(event);
  if (entry !== null) next.conversation = appendConversation(next.conversation, entry);

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
  const last = previous.at(-1);
  if (
    last &&
    incoming.kind === 'tool' &&
    last.kind === 'tool' &&
    last.invocationId === incoming.invocationId &&
    !incoming.startsTurn
  ) {
    const separator = last.content.endsWith('\n') || incoming.content.startsWith('\n') ? '' : '\n';
    return [
      ...previous.slice(0, -1),
      {
        ...last,
        content: last.content + separator + incoming.content,
        toolResponse:
          (last.toolResponse ?? '') + (last.toolResponse ? separator : '') + incoming.content,
      },
    ];
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
  return `${state.status} · ${state.agentKind ?? 'starting'} · ${state.roundLabel ?? 'no round yet'}`;
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

export function visibleRoundNumber(state: SessionState): number | null {
  return visibleRunMapRoundNumber(state.rounds, state.selectedRound);
}

function renderEventForTranscript(event: RunEvent): string | null {
  const data = event.data;
  if (data?.kind === 'agent_output_chunk' || data?.kind === 'subprocess_output') {
    return data.content;
  }
  if (data?.kind === 'output') return data.content;
  if (data?.kind === 'judge_result') {
    return `Judge: ${data.verdict.toUpperCase()}${data.feedback ? ` — ${data.feedback}` : ''}\n`;
  }
  if (data?.kind === 'benchmark_result') {
    return `Benchmark: ${data.metric}=${data.value} ${data.unit}\n`;
  }
  if (data?.kind === 'round_finished') {
    return `Round finished: ${data.judge_verdict.toUpperCase()} after ${data.attempts} attempt(s)\n`;
  }
  if (event.type === 'phase_started') {
    return `\n[${event.round_label ?? 'run'}] ${event.agent_kind ?? 'phase'} started\n`;
  }
  if (event.type === 'run_failed' || event.type === 'run_interrupted') {
    return `\nRun failed: ${event.text || 'interrupted'}\n`;
  }
  return null;
}

function appendTranscript(previous: string, next: string): string {
  const current = previous === 'Waiting for run events…' ? '' : previous;
  return `${current}${next}`.slice(-500_000);
}
