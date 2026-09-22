import {
  PersistentEventStream,
  type ProtocolResponse,
  type RequestInput,
  type RunEvent,
  type ServerMessage,
  type StreamTransport,
} from '@vibesys/backend-client/browser';
import {
  type CoreState,
  initialCoreState,
  reduceEvent,
  reduceEventBatch,
  reduceEventPrefix,
  reduceEventRebootstrap,
  reduceSnapshot,
} from '@vibesys/core-state';

export interface WorkspaceClient extends StreamTransport {
  request(input: RequestInput): Promise<ProtocolResponse>;
  close(): Promise<void>;
}

export interface QueryState {
  response: ProtocolResponse | null;
  loading: boolean;
  error: string | null;
}

export type QueryName = 'experiments' | 'performance' | 'design';
type Command = Extract<RequestInput, {type?: 'command.pause' | 'command.resume' | 'command.steer'}>;

export interface WorkspaceState {
  core: CoreState;
  runId: string | null;
  connection: 'connecting' | 'connected' | 'disconnected' | 'error';
  connectionError: string | null;
  snapshotError: string | null;
  queries: Record<QueryName, QueryState>;
  command: {sending: boolean; ack: ProtocolResponse['ack']; error: string | null};
  historyLoading: boolean;
  historyError: string | null;
}

const emptyQuery = (): QueryState => ({response: null, loading: true, error: null});

/** Owns browser subscriptions and query state; core-state owns the event projection. */
export class WorkspaceSession {
  #state: WorkspaceState = {
    core: initialCoreState(),
    runId: null,
    connection: 'connecting',
    connectionError: null,
    snapshotError: null,
    queries: {experiments: emptyQuery(), performance: emptyQuery(), design: emptyQuery()},
    command: {sending: false, ack: null, error: null},
    historyLoading: false,
    historyError: null,
  };
  #listeners = new Set<() => void>();
  #stream: PersistentEventStream | null = null;
  #reconnecting = false;
  #closed = false;
  #declaredFloor: number | null = null;
  #historyGeneration = 0;
  #runGeneration = 0;
  #spine = new Set<number>();
  #fetches = new Map<QueryName, Promise<void>>();
  #refreshPending = new Set<QueryName>();

  constructor(private readonly client: WorkspaceClient) {}

  getSnapshot = (): WorkspaceState => this.#state;

  subscribe = (listener: () => void): (() => void) => {
    this.#listeners.add(listener);
    return () => this.#listeners.delete(listener);
  };

  async start(): Promise<void> {
    await Promise.all([this.reconnect(), this.refresh()]);
  }

  async reconnect(): Promise<void> {
    if (this.#closed || this.#reconnecting) return;
    this.#reconnecting = true;
    try {
      this.#set({connection: 'connecting', connectionError: null});
      const old = this.#stream;
      this.#stream = null;
      await old?.close();
      if (this.#closed) return;
      // Each new stream bootstraps; automatic reconnects resume its existing cursor.
      this.#declaredFloor = null;
      const stream = new PersistentEventStream(this.client, {tail: 300});
      this.#stream = stream;
      await stream.subscribe({
        cursor: () => this.#state.core.sequence,
        shouldReconnect: () => !this.#closed && this.#state.connection !== 'error',
        onMessage: (message, {resumed}) => {
          if (this.#stream === stream) this.#onMessage(message, resumed);
        },
        onConnectionState: state => {
          if (this.#stream !== stream || this.#state.connection === 'error') return;
          this.#set({
            connection: state.status,
            connectionError: state.status === 'disconnected' ? state.error.message : null,
          });
          if (state.status === 'connected') void this.refresh();
        },
      });
    } finally {
      this.#reconnecting = false;
    }
  }

  async refresh(): Promise<void> {
    await Promise.all([
      this.#snapshot(),
      this.load('experiments'),
      this.load('performance'),
      this.load('design'),
    ]);
  }

  async #snapshot(): Promise<void> {
    const generation = this.#runGeneration;
    try {
      const response = await this.client.request({type: 'query.snapshot'});
      if (generation !== this.#runGeneration) return;
      if (!response.snapshot) throw new Error('The backend returned no run snapshot.');
      // Subscription identity owns the event cursor. A query cannot switch it.
      if (this.#state.runId !== null && response.snapshot.run_id !== this.#state.runId) return;
      this.#set({
        core: reduceSnapshot(this.#state.core, response.snapshot),
        runId: response.snapshot.run_id,
        snapshotError: null,
      });
    } catch (error) {
      if (generation !== this.#runGeneration) return;
      this.#set({snapshotError: errorMessage(error)});
    }
  }

  load(name: QueryName): Promise<void> {
    const existing = this.#fetches.get(name);
    if (existing) {
      this.#refreshPending.add(name);
      return existing;
    }
    this.#query(name, {...this.#state.queries[name], loading: true, error: null});
    const generation = this.#runGeneration;
    const fetch = this.client
      .request({type: `query.${name}`})
      .then(
        response => {
          if (generation !== this.#runGeneration) return;
          this.#query(name, {response, loading: false, error: null});
        },
        error => {
          if (generation !== this.#runGeneration) return;
          this.#query(name, {
            ...this.#state.queries[name],
            loading: false,
            error: errorMessage(error),
          });
        },
      )
      .finally(() => {
        if (generation !== this.#runGeneration) return;
        this.#fetches.delete(name);
        if (this.#refreshPending.delete(name) && !this.#closed) void this.load(name);
      });
    this.#fetches.set(name, fetch);
    return fetch;
  }

  async command(input: Command): Promise<boolean> {
    if (this.#state.command.sending || this.#state.connection !== 'connected') return false;
    const generation = this.#runGeneration;
    this.#set({command: {sending: true, ack: null, error: null}});
    try {
      const response = await this.client.request(input);
      if (generation !== this.#runGeneration) return false;
      if (!response.ack) throw new Error('The backend returned no command acknowledgment.');
      this.#set({command: {sending: false, ack: response.ack, error: null}});
      // An acknowledgment is not a lifecycle transition. Only events/snapshots set status.
      return true;
    } catch (error) {
      if (generation !== this.#runGeneration) return false;
      this.#set({command: {sending: false, ack: null, error: errorMessage(error)}});
      return false;
    }
  }

  async loadOlder(): Promise<void> {
    const floor = this.#state.core.historyAfterSequence;
    if (floor === 0 || this.#state.historyLoading) return;
    const generation = this.#historyGeneration;
    const nextFloor = Math.max(0, floor - 500);
    this.#set({historyLoading: true, historyError: null});
    try {
      const response = await this.client.request({
        type: 'query.events',
        after_sequence: nextFloor,
        before_sequence: floor + 1,
      });
      if (generation !== this.#historyGeneration) return;
      const events = (response.events ?? []).filter(
        event => event.sequence === undefined || !this.#spine.has(event.sequence),
      );
      this.#set({core: reduceEventPrefix(this.#state.core, events, nextFloor)});
    } catch (error) {
      if (generation !== this.#historyGeneration) return;
      this.#set({historyError: errorMessage(error)});
    } finally {
      if (generation === this.#historyGeneration) this.#set({historyLoading: false});
    }
  }

  async close(): Promise<void> {
    this.#closed = true;
    this.#listeners.clear();
    await this.#stream?.close();
    await this.client.close();
  }

  #onMessage(message: ServerMessage, resumed: boolean): void {
    if (message.type === 'subscribed') {
      const changed = this.#state.runId !== null && this.#state.runId !== message.run_id;
      if (changed) {
        this.#runGeneration += 1;
        this.#historyGeneration += 1;
        this.#declaredFloor = null;
        this.#spine.clear();
        this.#fetches.clear();
        this.#refreshPending.clear();
        this.#set({
          runId: message.run_id,
          core: initialCoreState(),
          snapshotError: null,
          queries: {experiments: emptyQuery(), performance: emptyQuery(), design: emptyQuery()},
          command: {sending: false, ack: null, error: null},
          historyLoading: false,
          historyError: null,
        });
        // A resume requested the old run's cursor and can omit the entire new run.
        // Replacing the stream also rejects remaining messages from that dial.
        if (resumed) {
          void this.reconnect();
          void this.refresh();
          return;
        }
        void this.refresh();
      }
      this.#set({runId: message.run_id, connection: 'connected', connectionError: null});
    } else if (message.type === 'protocol_error') {
      this.#set({connection: 'error', connectionError: `${message.code}: ${message.message}`});
    } else if (message.type === 'event') {
      this.#set({core: reduceEvent(this.#state.core, message.event)});
      this.#invalidate([message.event]);
    } else if (message.type === 'event_batch') {
      const declared = message.history_after_sequence ?? 0;
      const reset = !resumed && (this.#declaredFloor === null || declared > this.#declaredFloor);
      let floor = this.#state.core.historyAfterSequence;
      if (!resumed) {
        this.#declaredFloor = declared;
        floor = reset ? declared : Math.min(floor, declared);
        if (reset) {
          this.#historyGeneration += 1;
          this.#spine.clear();
        }
        for (const event of message.events) {
          if (event.sequence !== undefined && event.sequence <= declared)
            this.#spine.add(event.sequence);
        }
      }
      const fold = reset ? reduceEventRebootstrap : reduceEventBatch;
      this.#set({
        core: fold(
          this.#state.core,
          message.events,
          message.active_executions,
          message.through_sequence,
          floor,
        ),
      });
      this.#invalidate(message.events);
      // A bootstrap can replace a concurrently loaded snapshot. Refresh liveness after it.
      if (reset) void this.#snapshot();
    }
  }

  #invalidate(events: readonly RunEvent[]): void {
    if (events.some(event => event.type === 'experiments_changed')) {
      void this.load('experiments');
      void this.load('design');
      void this.load('performance');
    } else if (
      events.some(event => event.type === 'round_finished' || event.type === 'benchmark_result')
    ) {
      void this.load('performance');
    }
  }

  #query(name: QueryName, query: QueryState): void {
    this.#set({queries: {...this.#state.queries, [name]: query}});
  }

  #set(patch: Partial<WorkspaceState>): void {
    if (this.#closed) return;
    this.#state = {...this.#state, ...patch};
    for (const listener of this.#listeners) listener();
  }
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
