/** What Start sends to complete a run's setup: the task made, and the limits the optimizing run starts with. */
import type {Agent, Harness} from '@vibesys/orchestrator-proto';
import type {SetupFacts} from '../../../state/index.js';
import {DEFAULT_FANOUT, NO_STEERING} from '../start-draft.js';

export interface CompleteSetupRequest {
  readonly task: string;
  readonly fanoutLimit: number;
  readonly lead: {readonly harness: Harness; readonly model: string};
  readonly brief: string;
  /** The eval place, empty to let the host resolve it. */
  readonly evalPlace: string;
  /** How long the optimizing run may take, in seconds; null for no limit. */
  readonly maxTimeSeconds: number | null;
  /** The most the optimizing run may cost in dollars; null for no limit. */
  readonly maxCostUsd: number | null;
}

/**
 * The request Start makes from what setup made: the declared task, the lead the chat ran on, the default
 * fanout, and no limits (the run's settings change them once it is optimizing). The host reads the task
 * itself and decides whether setup is complete; this names nothing it derived.
 */
export function completeRequestOf(facts: SetupFacts, lead: Agent): CompleteSetupRequest {
  return {
    task: facts.task,
    fanoutLimit: DEFAULT_FANOUT,
    lead: {harness: lead.harness, model: lead.model},
    brief: NO_STEERING,
    evalPlace: '',
    maxTimeSeconds: null,
    maxCostUsd: null,
  };
}
