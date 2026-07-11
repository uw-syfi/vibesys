import type {RunEvent, RunSnapshot} from './protocol.js';

export interface SessionState {
  sequence: number;
  status: string;
  agentKind: string | null;
  roundLabel: string | null;
  liveContent: string;
  conversation: ConversationEntry[];
  detailContent: string;
  view: 'live' | 'detail' | 'help' | 'error';
  terminal: boolean;
}

export interface ConversationEntry {
  id: string;
  kind: 'assistant' | 'analysis' | 'tool' | 'subprocess' | 'status' | 'result';
  content: string;
  label?: string;
  tone?: 'normal' | 'success' | 'failure';
}

export function initialSessionState(): SessionState {
  return {
    sequence: 0,
    status: 'connecting',
    agentKind: null,
    roundLabel: null,
    liveContent: 'Waiting for run events…',
    conversation: [],
    detailContent: '',
    view: 'live',
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
  let next = {...state, sequence: Math.max(state.sequence, sequence)};
  if (event.agent_kind) next.agentKind = event.agent_kind;
  if (event.round_label) next.roundLabel = event.round_label;

  const line = renderEventForTranscript(event);
  if (line !== null) next.liveContent = appendTranscript(next.liveContent, line);
  const entry = eventToConversationEntry(event);
  if (entry !== null) next.conversation = appendConversation(next.conversation, entry);

  if (event.type === 'run_started') next.status = 'running';
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

function eventToConversationEntry(event: RunEvent): ConversationEntry | null {
  const data = event.data;
  const id = String(event.sequence ?? `${event.timestamp}-${event.type}`);
  if (data?.kind === 'agent_output_chunk') {
    const kind = data.channel === 'assistant'
      ? 'assistant'
      : data.channel === 'analysis' ? 'analysis' : 'tool';
    return {id, kind, content: data.content, label: labelFor(event, data.channel)};
  }
  if (data?.kind === 'subprocess_output') {
    return {
      id,
      kind: 'subprocess',
      content: data.content,
      label: `${data.process_kind} · ${data.stream}`,
    };
  }
  if (event.type === 'phase_started') {
    return {id, kind: 'status', content: 'started', label: labelFor(event, 'phase')};
  }
  if (data?.kind === 'judge_result') {
    return {
      id,
      kind: 'result',
      content: data.feedback || `Judge returned ${data.verdict}.`,
      label: `Judge · ${data.verdict.toUpperCase()}`,
      tone: data.verdict === 'pass' ? 'success' : 'failure',
    };
  }
  if (data?.kind === 'benchmark_result') {
    return {
      id,
      kind: 'result',
      content: `${data.metric}: ${data.value} ${data.unit}`,
      label: 'Benchmark',
      tone: 'success',
    };
  }
  if (data?.kind === 'round_finished') {
    return {
      id,
      kind: 'result',
      content: `${data.attempts} attempt(s)`,
      label: `${event.round_label ?? 'Round'} · ${data.judge_verdict.toUpperCase()}`,
      tone: data.judge_verdict === 'pass' ? 'success' : 'failure',
    };
  }
  if (event.type === 'run_failed' || event.type === 'run_interrupted') {
    return {id, kind: 'result', content: event.text || 'Run interrupted.', label: 'Run failed', tone: 'failure'};
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
  if (last && last.kind === incoming.kind && last.label === incoming.label
    && (incoming.kind === 'assistant' || incoming.kind === 'analysis'
      || incoming.kind === 'tool' || incoming.kind === 'subprocess')) {
    return [...previous.slice(0, -1), {...last, content: last.content + incoming.content}];
  }
  return [...previous, incoming].slice(-1_000);
}

export function showLive(state: SessionState): SessionState {
  return {...state, view: 'live'};
}

export function showDetail(
  state: SessionState,
  detailContent: string,
  view: SessionState['view'] = 'detail',
): SessionState {
  return {...state, detailContent, view};
}

export function statusText(state: SessionState): string {
  return `${state.status} · ${state.agentKind ?? 'starting'} · ${state.roundLabel ?? 'no round yet'}`;
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
