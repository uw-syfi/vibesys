import type {EventSubscription} from './client.js';
import {HELP_TEXT, parseInput} from './commands.js';
import type {ProtocolResponse, RequestInput, ServerMessage} from './protocol.js';
import {
  applyEvent,
  applySnapshot,
  initialSessionState,
  selectNextAgent,
  selectNextRound,
  selectPreviousAgent,
  selectPreviousRound,
  selectRound,
  type SessionState,
  showDetail,
  showLive,
} from './session-model.js';

export interface SessionController {
  readonly state: SessionState;
  start(): Promise<void>;
  stop(): Promise<void>;
  submit(value: string): Promise<void>;
  live(): void;
  selectNextAgent(): void;
  selectPreviousAgent(): void;
  selectNextRound(): void;
  selectPreviousRound(): void;
  selectRound(roundNumber: number): void;
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

  async submit(value: string): Promise<void> {
    const parsed = parseInput(value.trim());
    if (parsed.error) return this.#setState(showDetail(this.#state, parsed.error, 'error'));
    if (parsed.localView === 'help') {
      return this.#setState(showDetail(this.#state, HELP_TEXT, 'help'));
    }
    if (!parsed.request) return;
    try {
      const response = await this.client.request(parsed.request);
      const rendered = renderResponse(parsed.request, response);
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

function renderResponse(request: RequestInput, response: ProtocolResponse): string | null {
  if (response.ack) return `${response.ack.action}: ${response.ack.status}`;
  if (response.chat) return `you: ${response.chat.question}\nvibesys: ${response.chat.answer}`;
  if (request.type === 'query.history') return renderRoundHistory(response.events ?? []);
  return null;
}

export function renderRoundHistory(events: ProtocolResponse['events'], now = new Date()): string {
  const rounds = new Map<
    number,
    {
      startedAt: Date;
      finishedAt?: Date;
      status: 'running' | 'completed' | 'failed';
      phases: Set<string>;
    }
  >();
  for (const event of events ?? []) {
    const match = event.round_label?.match(/^round-(\d+)/);
    if (!match) continue;
    const round = Number(match[1]);
    const timestamp = new Date(event.timestamp);
    const current = rounds.get(round);
    if (!current || timestamp < current.startedAt) {
      rounds.set(round, {
        phases: current?.phases ?? new Set(),
        status: current?.status ?? 'running',
        ...(current?.finishedAt ? {finishedAt: current.finishedAt} : {}),
        startedAt: timestamp,
      });
    }
    const updated = rounds.get(round);
    if (updated && event.agent_kind) updated.phases.add(event.agent_kind);
    if (updated && (event.type === 'run_failed' || event.type === 'run_interrupted')) {
      updated.status = 'failed';
      updated.finishedAt = timestamp;
    }
    if (event.type === 'round_finished') {
      const updated = rounds.get(round);
      if (updated) {
        updated.finishedAt = timestamp;
        updated.status = event.status === 'failed' ? 'failed' : 'completed';
      }
    }
  }
  if (rounds.size === 0) return 'No rounds have started yet.';
  const lines = ['Rounds'];
  for (const [round, timing] of [...rounds.entries()].sort(([a], [b]) => a - b)) {
    const end = timing.finishedAt ?? now;
    const elapsedSeconds = Math.max(
      0,
      Math.floor((end.getTime() - timing.startedAt.getTime()) / 1000),
    );
    const phases = [...timing.phases].join(' -> ') || 'no agent phases yet';
    lines.push(`Round ${round} · ${timing.status} · ${formatDuration(elapsedSeconds)} · ${phases}`);
  }
  return lines.join('\n');
}

function formatDuration(totalSeconds: number): string {
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  if (hours > 0) return `${hours}h ${minutes}m ${seconds}s`;
  if (minutes > 0) return `${minutes}m ${seconds}s`;
  return `${seconds}s`;
}
