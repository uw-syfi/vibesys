import type {OrchestratorState} from './orchestrator/state.js';

/**
 * Whether `runId` has the managed orchestrator capability set. A route-shaped id alone proves no
 * such thing: generic-index runs deliberately use different history, cost and detail streams.
 */
export const isManagedRun = (
  state: OrchestratorState,
  runId: string | undefined,
): runId is string => runId !== undefined && state.runs[runId] !== undefined;
