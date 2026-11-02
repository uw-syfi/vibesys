/**
 * Where an orchestrator's facts come from, as the screens see it. The real source reads a remote
 * daemon over `ssh <host> vibesys-orchestrator connect`; a fixture source replays a scripted run.
 * A screen takes this interface only, so the real source plugs in with no screen change.
 *
 * Every stream ends in one of three ways: it finishes (the daemon closed it), it is cancelled
 * (the caller's `signal` aborted, and the iterable ends quietly), or it fails (`SourceError`).
 */
import type {
  Command,
  ItemsPage,
  Plan,
  Receipt,
  WatchAgentResponse,
  WatchRunsResponse,
} from '@vibesys/orchestrator-proto';

/** The part of `AbortSignal` the sources use, so a neutral entry needs no DOM library. */
export interface CancelSignal {
  readonly aborted: boolean;
  addEventListener(type: 'abort', listener: () => void, options?: {once?: boolean}): void;
}

export class SourceError extends Error {
  constructor(
    /** `lost` the connection went away; `refused` the daemon answered no; `invalid` it sent a bad frame. */
    readonly kind: 'lost' | 'refused' | 'invalid',
    message: string,
  ) {
    super(message);
    this.name = 'SourceError';
  }
}

export interface OrchestratorSource {
  /** A snapshot when `afterSequence` is 0n, then every event after it. */
  watchRuns(afterSequence: bigint, signal: CancelSignal): AsyncIterable<WatchRunsResponse['body']>;
  /**
   * A page of the newest items when `afterSequence` is 0n, then items, progress and turns. Each
   * response is whole, because its `sequence` is what tells two copies of an item apart.
   */
  watchAgent(
    agentId: string,
    pageSize: number,
    afterSequence: bigint,
    signal: CancelSignal,
  ): AsyncIterable<WatchAgentResponse>;
  /** Items below `beforeOrdinal`, newest first. */
  listItems(
    agentId: string,
    beforeOrdinal: number,
    pageSize: number,
    signal: CancelSignal,
  ): Promise<ItemsPage>;
  listPlans(runId: string, signal: CancelSignal): Promise<readonly Plan[]>;
  /** Carry out a command as the operator. The same id answers with the same receipt. */
  dispatch(command: Command, signal: CancelSignal): Promise<Receipt>;
}
