/**
 * The operations a host connection machine asks for, over any `OrchestratorSource`. A watch owns
 * the cancellation of its stream, so the code that holds one only ever calls `close`; a request
 * takes the signal of whoever asked, and the ports own the connection itself, released by `close`.
 */
import type {
  Command,
  ItemsPage,
  Plan,
  Receipt,
  WatchAgentResponse,
  WatchRunsResponse,
} from '@vibesys/orchestrator-proto';
import type {CancelSignal, OrchestratorSource} from './source.js';

export interface Watch<T> {
  readonly frames: AsyncIterable<T>;
  /** Stop the stream; its `frames` end quietly. Safe to call twice. */
  close(): void;
}

export interface HostPorts {
  openRuns(afterSequence: bigint): Watch<WatchRunsResponse['body']>;
  openAgent(agentId: string, pageSize: number, afterSequence: bigint): Watch<WatchAgentResponse>;
  listItems(
    agentId: string,
    beforeOrdinal: number,
    pageSize: number,
    signal: CancelSignal,
  ): Promise<ItemsPage>;
  listPlans(runId: string, signal: CancelSignal): Promise<readonly Plan[]>;
  send(command: Command, signal: CancelSignal): Promise<Receipt>;
  /** Release the connection and whatever process carries it. Open streams and requests end as lost. */
  close(): void;
}

function watchOf<T>(open: (signal: CancelSignal) => AsyncIterable<T>): Watch<T> {
  const controller = new AbortController();
  return {frames: open(controller.signal), close: () => controller.abort()};
}

/** `release` frees what the source holds open (a child process); a fixture holds nothing. */
export function portsOf(
  source: OrchestratorSource,
  release: () => void = () => undefined,
): HostPorts {
  return {
    openRuns: after => watchOf(signal => source.watchRuns(after, signal)),
    openAgent: (agentId, pageSize, after) =>
      watchOf(signal => source.watchAgent(agentId, pageSize, after, signal)),
    listItems: (agentId, before, pageSize, signal) =>
      source.listItems(agentId, before, pageSize, signal),
    listPlans: (runId, signal) => source.listPlans(runId, signal),
    send: (command, signal) => source.dispatch(command, signal),
    close: release,
  };
}
