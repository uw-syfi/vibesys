import type {RunEvent, RunSnapshot} from './protocol.js';

export interface SessionState {
  sequence: number;
  status: string;
  agentKind: string | null;
  roundLabel: string | null;
  liveContent: string;
  detailContent: string;
  view: 'live' | 'detail' | 'help' | 'error';
  terminal: boolean;
}

export function initialSessionState(): SessionState {
  return {
    sequence: 0,
    status: 'connecting',
    agentKind: null,
    roundLabel: null,
    liveContent: 'Waiting for run events…',
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
