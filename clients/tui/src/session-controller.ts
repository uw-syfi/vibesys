import type {EventSubscription} from './client.js';
import {HELP_TEXT, parseInput} from './commands.js';
import {renderPerformanceCurve} from './performance-chart.js';
import type {ProtocolResponse, RequestInput, ServerMessage} from './protocol.js';
import {
  activeTimingElapsedMs,
  closeActiveAgentTimings,
  finishAgentTiming,
  type RoundTimingState,
  startAgentTiming,
} from './round-timing.js';
import {
  applyEvent,
  applySnapshot,
  type ConversationEntry,
  initialSessionState,
  type SessionState,
  selectNextAgent,
  selectNextRound,
  selectPreviousAgent,
  selectPreviousRound,
  selectRound,
  showDetail,
  showLive,
  toggleTodos,
} from './session-model.js';

export interface SessionController {
  readonly state: SessionState;
  start(): Promise<void>;
  stop(): Promise<void>;
  submit(value: string): Promise<void>;
  closeChat(): void;
  sendChat(value: string): Promise<void>;
  live(): void;
  selectNextAgent(): void;
  selectPreviousAgent(): void;
  selectNextRound(): void;
  selectPreviousRound(): void;
  selectRound(roundNumber: number): void;
  toggleTodos(): void;
  subscribe(listener: (state: SessionState) => void): () => void;
}

export interface SupervisionTransport {
  request(input: RequestInput): Promise<ProtocolResponse>;
  subscribe(
    afterSequence: number,
    onMessage: (message: ServerMessage) => void,
    onDisconnect: (error: Error) => void,
  ): Promise<EventSubscription>;
  close(): Promise<void>;
}

export class SocketSessionController implements SessionController {
  #state = initialSessionState();
  readonly #listeners = new Set<(state: SessionState) => void>();
  #eventSubscription: EventSubscription | null = null;
  #chatMessageId = 0;
  readonly #chatQueue: Array<{id: string; text: string}> = [];
  #chatDrain: Promise<void> | null = null;

  constructor(private readonly client: SupervisionTransport) {}

  get state(): SessionState {
    return this.#state;
  }

  async start(): Promise<void> {
    const response = await this.client.request({type: 'query.snapshot'});
    if (response.snapshot) this.#setState(applySnapshot(this.#state, response.snapshot));
    this.#eventSubscription = await this.client.subscribe(
      0,
      message => this.#onMessage(message),
      error => {
        if (!this.#state.terminal) this.#setState(showDetail(this.#state, String(error), 'error'));
      },
    );
  }

  async stop(): Promise<void> {
    await this.#eventSubscription?.close();
    this.#eventSubscription = null;
    await this.client.close();
  }

  subscribe(listener: (state: SessionState) => void): () => void {
    this.#listeners.add(listener);
    listener(this.#state);
    return () => this.#listeners.delete(listener);
  }

  live(): void {
    this.#setState(showLive(this.#state));
  }

  selectNextAgent(): void {
    this.#setState(selectNextAgent(this.#state));
  }

  selectPreviousAgent(): void {
    this.#setState(selectPreviousAgent(this.#state));
  }

  selectNextRound(): void {
    this.#setState(selectNextRound(this.#state));
  }

  selectPreviousRound(): void {
    this.#setState(selectPreviousRound(this.#state));
  }

  selectRound(roundNumber: number): void {
    this.#setState(selectRound(this.#state, roundNumber));
  }

  toggleTodos(): void {
    this.#setState(toggleTodos(this.#state));
  }

  closeChat(): void {
    this.#setState({...this.#state, chatOpen: false});
  }

  sendChat(value: string): Promise<void> {
    const text = value.trim();
    if (!text) return Promise.resolve();
    const id = `chat-user-${++this.#chatMessageId}`;
    const queued = this.#state.chatPending || this.#chatQueue.length > 0;
    this.#chatQueue.push({id, text});
    this.#setState({
      ...this.#state,
      chatOpen: true,
      chatConversation: appendChatEntry(this.#state.chatConversation, {
        id,
        kind: 'user',
        label: queued ? 'You · queued' : 'You',
        content: text,
      }),
    });
    if (this.#chatDrain === null) {
      const drain = this.#drainChatQueue();
      this.#chatDrain = drain.finally(() => {
        this.#chatDrain = null;
      });
    }
    return this.#chatDrain;
  }

  async #drainChatQueue(): Promise<void> {
    try {
      while (this.#chatQueue.length > 0) {
        const messages = this.#chatQueue.splice(0);
        const messageIds = new Set(messages.map(message => message.id));
        this.#setState({
          ...this.#state,
          chatPending: true,
          chatConversation: this.#state.chatConversation.map(entry =>
            messageIds.has(entry.id) ? {...entry, label: 'You'} : entry,
          ),
        });
        await this.#requestChat(messages.map(message => message.text).join('\n\n'));
      }
    } finally {
      this.#setState({...this.#state, chatPending: false});
    }
  }

  async #requestChat(text: string): Promise<void> {
    try {
      const response = await this.client.request({type: 'query.chat', text});
      const answer = response.chat?.answer ?? 'No chat answer was returned.';
      let state = this.#state;
      for (const event of response.events ?? []) state = applyEvent(state, event);
      if (!(response.events ?? []).some(event => event.data?.kind === 'chat')) {
        state = {
          ...state,
          chatConversation: appendChatEntry(state.chatConversation, {
            id: `chat-answer-${++this.#chatMessageId}`,
            kind: 'assistant',
            label: 'Answer',
            content: answer,
          }),
        };
      }
      this.#setState(state);
    } catch (error) {
      this.#setState({
        ...this.#state,
        chatConversation: appendChatEntry(this.#state.chatConversation, {
          id: `chat-error-${++this.#chatMessageId}`,
          kind: 'result',
          label: 'Chat failed',
          tone: 'failure',
          content: String(error),
        }),
      });
    }
  }

  async submit(value: string): Promise<void> {
    const parsed = parseInput(value.trim());
    if (parsed.error) return this.#setState(showDetail(this.#state, parsed.error, 'error'));
    if (parsed.localView === 'help') {
      return this.#setState(showDetail(this.#state, HELP_TEXT, 'help'));
    }
    if (parsed.localView === 'chat') {
      this.#setState({...this.#state, overlay: null, chatOpen: true});
      if (parsed.chatMessage) await this.sendChat(parsed.chatMessage);
      return;
    }
    if (!parsed.request) return;
    try {
      const response = await this.client.request(parsed.request);
      const rendered = renderResponse(parsed.request, response, parsed.responseView);
      if (rendered !== null) this.#setState(showDetail(this.#state, rendered));
    } catch (error) {
      this.#setState(showDetail(this.#state, String(error), 'error'));
    }
  }

  #onMessage(message: ServerMessage): void {
    if (message.type === 'event') this.#setState(applyEvent(this.#state, message.event));
    if (message.type === 'event_batch') {
      let state = this.#state;
      for (const event of message.events) state = applyEvent(state, event);
      this.#setState(state);
    }
    if (message.type === 'protocol_error') {
      this.#setState(showDetail(this.#state, message.message, 'error'));
    }
  }

  #setState(state: SessionState): void {
    this.#state = state;
    for (const listener of this.#listeners) listener(state);
  }
}

function appendChatEntry(
  conversation: ConversationEntry[],
  entry: ConversationEntry,
): ConversationEntry[] {
  return [...conversation, entry].slice(-500);
}

function renderResponse(
  request: RequestInput,
  response: ProtocolResponse,
  responseView?: 'history' | 'perf',
): string | null {
  if (response.ack) return `${response.ack.action}: ${response.ack.status}`;
  if (request.type === 'query.performance' || responseView === 'perf') {
    return renderPerformanceCurve(response.performance ?? [], response.events ?? []);
  }
  if (request.type === 'query.history') return renderRoundHistory(response.events ?? []);
  return null;
}

export function renderRoundHistory(events: ProtocolResponse['events'], now = new Date()): string {
  const rounds = new Map<number, HistoryRound>();
  for (const event of events ?? []) {
    const match = event.round_label?.match(/^round-(\d+)/);
    if (!match) continue;
    const round = Number(match[1]);
    rounds.set(round, applyHistoryEvent(rounds.get(round), event));
  }
  if (rounds.size === 0) return 'No rounds have started yet.';
  const lines = ['Rounds'];
  for (const [round, history] of [...rounds.entries()].sort(([a], [b]) => a - b)) {
    const elapsedMs = activeTimingElapsedMs(history.timing, now);
    const elapsedSeconds = Math.max(0, Math.floor(elapsedMs / 1000));
    const phases = [...history.phases].join(' -> ') || 'no agent phases yet';
    lines.push(
      `Round ${round} · ${history.status} · ${formatDuration(elapsedSeconds)} · ${phases}`,
    );
  }
  return lines.join('\n');
}

interface HistoryRound {
  startedAt: string;
  finishedAt?: string;
  status: 'running' | 'completed' | 'failed';
  phases: Set<string>;
  timing: RoundTimingState;
}

function applyHistoryEvent(
  current: HistoryRound | undefined,
  event: NonNullable<ProtocolResponse['events']>[number],
): HistoryRound {
  let next: HistoryRound = {
    phases: current?.phases ?? new Set(),
    status: current?.status ?? 'running',
    timing: current?.timing ?? {},
    ...(current?.finishedAt ? {finishedAt: current.finishedAt} : {}),
    startedAt: earliestTimestamp(current?.startedAt, event.timestamp) ?? event.timestamp,
  };
  if (event.agent_kind) next.phases.add(event.agent_kind);
  if (event.type === 'phase_started')
    next = {...next, timing: startAgentTiming(next.timing, event)};
  if (event.type === 'phase_finished') {
    next = {...next, timing: finishAgentTiming(next.timing, event)};
  }
  if (event.type === 'run_failed' || event.type === 'run_interrupted') {
    return {
      ...next,
      status: 'failed',
      finishedAt: event.timestamp,
      timing: closeActiveAgentTimings(next.timing, event.timestamp),
    };
  }
  if (event.type === 'round_finished') {
    return {
      ...next,
      status: event.status === 'failed' ? 'failed' : 'completed',
      finishedAt: event.timestamp,
      timing: closeActiveAgentTimings(next.timing, event.timestamp),
    };
  }
  return next;
}

function earliestTimestamp(
  left: string | undefined,
  right: string | undefined,
): string | undefined {
  if (!left) return right;
  if (!right) return left;
  return new Date(right).getTime() < new Date(left).getTime() ? right : left;
}

function formatDuration(totalSeconds: number): string {
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  if (hours > 0) return `${hours}h ${minutes}m ${seconds}s`;
  if (minutes > 0) return `${minutes}m ${seconds}s`;
  return `${seconds}s`;
}
