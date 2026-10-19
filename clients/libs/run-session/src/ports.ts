/**
 * What the live session needs from its platform, as interfaces the composition implements: the
 * run's connection (the app's backend adapter over a WebSocket or the desktop shell's bridge) and
 * the page's lifecycle. Types only; the session's shell calls them and never names an
 * implementation.
 */
import type {
  EventBatchMessage,
  IterationResult,
  ProtocolResponse,
  RequestInput,
  RunEvent,
  RunSnapshot,
} from '@vibesys/contracts';
import type {
  AbortSignalLike,
  BatchReconciliation,
  ControlChannelState,
  StreamConnectionState,
  StreamMessageContext,
  StreamRebootstrapDisposition,
} from '@vibesys/transport';

/** Where a transport reports control-channel connectivity. */
export interface TransportHooks {
  readonly onConnectionState: (state: ControlChannelState) => void;
}

/** What the run's event stream reports to the session. */
export interface RunStreamHandlers {
  /** The last sequence folded, where a resumed subscription continues. */
  cursor(): number;
  /** Whether the caller's fold can still be incomplete, so a dropped stream is worth redialing. */
  shouldReconnect(): boolean;
  onBatch(
    message: EventBatchMessage,
    reconciliation: BatchReconciliation,
    context: StreamMessageContext,
  ): void;
  /** An in-band protocol error; the transport suppresses the close that follows it. */
  onFault(error: Error): void;
  onConnectionState(state: StreamConnectionState): void;
}

/** A lifecycle command the run protocol accepts on its control channel. */
export type RunCommand = Extract<RequestInput, {readonly type: `command.${string}`}>;

/** The protocol-neutral result of delivering a run command. The connection resolves wire-level
 * response shape here so presentation code receives only the fact it renders. */
export type RunCommandOutcome =
  | {
      readonly kind: 'acknowledged';
      readonly action: NonNullable<ProtocolResponse['ack']>['action'];
      readonly status: NonNullable<ProtocolResponse['ack']>['status'];
    }
  | {
      readonly kind: 'rejected';
      readonly message: string | null;
      /** The server's typed reason (a diagnostic code such as `run_read_only`), when it gave one. */
      readonly code?: string | null;
    };

/** Optional thread selection and caller-owned cancellation for one experiment-chat turn. */
export interface WebChatOptions {
  readonly threadId?: string;
  readonly signal?: AbortSignalLike;
}

export type ChatResult = NonNullable<ProtocolResponse['chat']>;

/** The provider and model a new chat thread runs, a pair from the host's harness catalogue. */
export interface ChatHarness {
  readonly provider: string;
  readonly model: string;
}

export type ChatThreadResult = NonNullable<ProtocolResponse['chat_thread']>;

/** The created thread and its authoritative response events, before the session folds them. */
export interface ChatThreadResponse {
  readonly thread: ChatThreadResult;
  readonly events: readonly RunEvent[];
}

/** The answer and its authoritative response events, before the session folds them. */
export interface ChatResponse {
  readonly chat: ChatResult;
  readonly events: readonly RunEvent[];
}

/** The run behind one control transport: its snapshot and its persistent event stream. */
export interface RunConnection {
  /**
   * Load an unfenced snapshot, or require the event store established by a stream batch. The
   * owning state aborts `signal` when a newer load, recovery, or close supersedes this request.
   */
  loadSnapshot(storeId: string | undefined, signal: AbortSignalLike): Promise<RunSnapshot>;
  /** Load one page of the policy-owned iteration tree from the stream's store generation. */
  loadIterations(
    cursor: string | null,
    expectedStoreId: string,
    signal: AbortSignalLike,
  ): Promise<IterationLoad>;
  /** Ask the run's experiment-chat agent, retaining events for the session fold. */
  ask(text: string, options?: WebChatOptions): Promise<ChatResponse>;
  /** Create a chat thread; `harness` null runs the run's own provider and model. */
  createChatThread(
    harness: ChatHarness | null,
    options?: {readonly signal?: AbortSignalLike},
  ): Promise<ChatThreadResponse>;
  /** Deliver one lifecycle command and resolve its wire response to a semantic outcome. */
  command(input: RunCommand): Promise<RunCommandOutcome>;
  /** Subscribe once; settles when the first subscription does. */
  subscribe(handlers: RunStreamHandlers): Promise<void>;
  /** Resume the stream where it was. */
  retry(): void;
  /** Retry only when the currently owned stream has already disconnected. */
  retryIfDisconnected(): void;
  /** Restart the stream from a fresh bootstrap. */
  rebootstrap(): StreamRebootstrapDisposition;
  /** Cancel an armed control-channel redial and dial now (inert when healthy or dialing). */
  redialControls(): void;
  close(): Promise<void>;
}

export type IterationLoad =
  | {readonly kind: 'result'; readonly result: IterationResult}
  | {readonly kind: 'not-ready'}
  | {readonly kind: 'unsupported'};

/** The page's visibility, as the browser reports it. */
export type PageVisibility = 'visible' | 'hidden';

/** The page's visibility and network reachability, and their change events. */
export interface BrowserLifecycle {
  readonly visibilityState: PageVisibility;
  readonly online: boolean;
  addEventListener(type: 'visibilitychange' | 'online' | 'offline', listener: () => void): void;
  removeEventListener(type: 'visibilitychange' | 'online' | 'offline', listener: () => void): void;
}
