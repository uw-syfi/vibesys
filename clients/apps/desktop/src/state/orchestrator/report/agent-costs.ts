/**
 * The agents of a run by type with what each type has cost, as one table: the host's count of agents
 * that are working and that the run has had (`RunSummary`), beside the host's turns, tokens and
 * cost of the type (`RunCost.roles`). Rows follow the cost view's order (most costly first) and then
 * the types that have cost nothing yet in the order the summary serves them. Nothing is added up.
 */
import type {AgentRole} from '@vibesys/orchestrator-proto';
import type {CostView} from '../cost.js';
import type {AgentsStrip} from './agents-strip.js';

export interface AgentCostRow {
  readonly role: AgentRole;
  /** Agents of the type that are working, as the host counts them; 0 before the summary is served. */
  readonly running: number;
  /** Agents of the type the run has had; the cost view's count before the summary is served. */
  readonly total: number;
  /** Finished turns, tokens and cost of the type; null while the cost view has not been read. */
  readonly turns: number | null;
  readonly tokens: number | null;
  /** The cost of the priced turns; null when none is priced, or while the cost view has not been read. */
  readonly costUsd: number | null;
  /** Turns of the type that reported tokens and no cost: its cost is a floor. */
  readonly unpricedTurns: number;
}

export function agentCostRows(
  agents: AgentsStrip,
  cost: CostView | undefined,
): readonly AgentCostRow[] {
  const counted = new Map(agents.types.map(type => [type.role, type] as const));
  const priced: AgentCostRow[] = (cost?.roles ?? []).map(role => ({
    role: role.role,
    running: counted.get(role.role)?.running ?? 0,
    total: counted.get(role.role)?.total ?? role.agents,
    turns: role.turns,
    tokens: role.totalTokens,
    costUsd: role.usage.costUsd,
    unpricedTurns: role.unpricedTurns,
  }));
  const listed = new Set(priced.map(row => row.role));
  const rest: AgentCostRow[] = agents.types
    .filter(type => !listed.has(type.role))
    .map(type => ({
      role: type.role,
      running: type.running,
      total: type.total,
      turns: cost === undefined ? null : 0,
      tokens: cost === undefined ? null : 0,
      costUsd: null,
      unpricedTurns: 0,
    }));
  return [...priced, ...rest];
}
