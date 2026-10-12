/**
 * What the live session needs from its platform, as interfaces the composition implements: the
 * run's connection (the app's backend adapter over a WebSocket or the desktop shell's bridge) and
 * the page's lifecycle. Types only; the session's shell calls them and never names an
 * implementation.
 */
import type {
  EventBatchMessage,
  ProtocolResponse,
  RequestInput,
  RunSnapshot,
  TuiDefaults,
} from '@vibesys/contracts';
import type {
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
  | {readonly kind: 'rejected'; readonly message: string | null};

/** The run behind one control transport: its snapshot and its persistent event stream. */
export interface RunConnection {
  /** Load an unfenced snapshot, or require the event store established by a stream batch. */
  loadSnapshot(storeId?: string): Promise<RunSnapshot>;
  /** Deliver one lifecycle command and resolve its wire response to a semantic outcome. */
  command(input: RunCommand): Promise<RunCommandOutcome>;
  /** Read the server's presentation defaults without making UI code name the transport. */
  tuiDefaults(): Promise<TuiDefaults | null>;
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

/** The page's visibility, as the browser reports it. */
export type PageVisibility = 'visible' | 'hidden';

/** The page's visibility and network reachability, and their change events. */
export interface BrowserLifecycle {
  readonly visibilityState: PageVisibility;
  readonly online: boolean;
  addEventListener(type: 'visibilitychange' | 'online' | 'offline', listener: () => void): void;
  removeEventListener(type: 'visibilitychange' | 'online' | 'offline', listener: () => void): void;
}
