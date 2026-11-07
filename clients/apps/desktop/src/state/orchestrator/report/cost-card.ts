/**
 * What the cost card of the report shows of a run's `CostView`: the total, the limit, the rate of the
 * last hour and the split by type of agent, each exactly as the host worked it out. The card adds
 * nothing up and prices nothing; a figure no turn reported is null, never zero.
 */
import type {AgentRole} from '@vibesys/orchestrator-proto';
import type {CostView} from '../cost.js';

export interface CostTypeRow {
  readonly role: AgentRole;
  readonly agents: number;
  readonly costUsd: number | null;
}

export interface CostCard {
  readonly totalUsd: number | null;
  /** The run's cost limit; null when it has none. */
  readonly limitUsd: number | null;
  /** What the turns of the last hour cost; null when none is priced. */
  readonly lastHourUsd: number | null;
  /** Turns reported tokens and no price: the total is a floor. */
  readonly partial: boolean;
  /** In the host's order, the costliest type first. */
  readonly types: readonly CostTypeRow[];
}

export function costCard(view: CostView): CostCard {
  return {
    totalUsd: view.total.costUsd,
    limitUsd: view.maxCostUsd,
    lastHourUsd: view.lastHourUsd,
    partial: view.unpricedTurns > 0,
    types: view.roles.map(role => ({
      role: role.role,
      agents: role.agents,
      costUsd: role.usage.costUsd,
    })),
  };
}
