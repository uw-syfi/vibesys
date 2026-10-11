/**
 * What the live session needs from its platform, as interfaces the composition implements: the
 * run's connection (the app's backend adapter over a WebSocket or the desktop shell's bridge) and
 * the page's lifecycle. Types only; the session's shell calls them and never names an
 * implementation.
 */
import type {EventBatchMessage, RunSnapshot} from '@vibesys/contracts';
import type {
  BatchReconciliation,
  ControlChannelState,
  StreamConnectionState,
} from '@vibesys/transport';

/** Where a transport reports control-channel connectivity. */
export interface TransportHooks {
  readonly onConnectionState: (state: ControlChannelState) => void;
}

/** What the run's event stream reports to the session. */
export interface RunStreamHandlers {
  /** The last sequence folded, where a resumed subscription continues. */
  cursor(): number;
  /** Whether a dropped stream is worth redialing (false once the run has ended). */
  shouldReconnect(): boolean;
  onBatch(message: EventBatchMessage, reconciliation: BatchReconciliation): void;
  /** An in-band protocol error; the transport suppresses the close that follows it. */
  onFault(error: Error): void;
  onConnectionState(state: StreamConnectionState): void;
}

/** The run behind one control transport: its snapshot and its persistent event stream. */
export interface RunConnection {
  loadSnapshot(): Promise<RunSnapshot>;
  /** Subscribe once; settles when the first subscription does. */
  subscribe(handlers: RunStreamHandlers): Promise<void>;
  /** Resume the stream where it was. */
  retry(): void;
  /** Restart the stream from a fresh bootstrap. */
  rebootstrap(): void;
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
