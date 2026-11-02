/**
 * The operations a host connection machine asks for, over any `OrchestratorSource`. A watch owns
 * the cancellation of its stream, so the code that holds one only ever calls `close`.
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
  openAgent(
    agentId: string,
    pageSize: number,
    afterSequence: bigint,
  ): Watch<WatchAgentResponse['body']>;
  listItems(agentId: string, beforeOrdinal: number, pageSize: number): Promise<ItemsPage>;
  listPlans(runId: string): Promise<readonly Plan[]>;
  send(command: Command): Promise<Receipt>;
}

function watchOf<T>(open: (signal: CancelSignal) => AsyncIterable<T>): Watch<T> {
  const controller = new AbortController();
  return {frames: open(controller.signal), close: () => controller.abort()};
}

export function portsOf(source: OrchestratorSource): HostPorts {
  // Calls that are not streams are never cancelled by the machine: a request that was sent is
  // answered, and a reply nobody wants is dropped by the state that asked.
  const never = new AbortController().signal;
  return {
    openRuns: after => watchOf(signal => source.watchRuns(after, signal)),
    openAgent: (agentId, pageSize, after) =>
      watchOf(signal => source.watchAgent(agentId, pageSize, after, signal)),
    listItems: (agentId, before, pageSize) => source.listItems(agentId, before, pageSize, never),
    listPlans: runId => source.listPlans(runId, never),
    send: command => source.dispatch(command, never),
  };
}
