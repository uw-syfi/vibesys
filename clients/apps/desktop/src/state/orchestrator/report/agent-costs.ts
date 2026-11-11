/**
 * The agents of a run by type with what each type has cost, as one table: the host's count of agents
 * that are working and that the run has had (`RunSummary`), beside the host's turns, tokens and
 * cost of the type (`RunCost.roles`). Rows follow the cost view's order (most costly first) and then
 * the types that have cost nothing yet in the order the summary serves them. Nothing is added up.
 */
import type {AgentRole} from '@vibesys/orchestrator-proto';
import type {CostUsage, CostView} from '../cost.js';
import type {AgentsStrip} from './agents-strip.js';

export interface AgentCostRow {
  readonly role: AgentRole;
  /** Agents of the type that are working, as the host counts them. */
  readonly running: number;
  /** Agents of the type the run has had, as the host counts them (the cost view's count before it serves a summary). */
  readonly total: number;
  /** What the type has cost; null while the cost view has not been read. */
  readonly spend: {
    /** Finished turns of the type. */
    readonly turns: number;
    /** Turns of the type that reported tokens and no cost: its cost is a floor. */
    readonly unpricedTurns: number;
    /** Tokens by kind, and the cost of the priced turns (null when none is priced). */
    readonly usage: CostUsage;
  } | null;
}

const NOTHING_YET: CostUsage = {
  inputTokens: null,
  cacheReadTokens: null,
  cacheCreationTokens: null,
  outputTokens: null,
  reasoningTokens: null,
  costUsd: null,
};

/**
 * The rows of the table. The agents are counted from the host's summary alone once it is served, so the
 * rows add up to the facts line's count; the cost view's own count of a type's agents stands in only
 * until a summary arrives.
 */
export function agentCostRows(
  agents: AgentsStrip,
  cost: CostView | undefined,
): readonly AgentCostRow[] {
  const counted = new Map(agents.types.map(type => [type.role, type] as const));
  const served = agents.types.length > 0;
  const priced: AgentCostRow[] = (cost?.roles ?? []).map(role => ({
    role: role.role,
    running: counted.get(role.role)?.running ?? 0,
    total: served ? (counted.get(role.role)?.total ?? 0) : role.agents,
    spend: {turns: role.turns, unpricedTurns: role.unpricedTurns, usage: role.usage},
  }));
  const listed = new Set(priced.map(row => row.role));
  const rest: AgentCostRow[] = agents.types
    .filter(type => !listed.has(type.role))
    .map(type => ({
      role: type.role,
      running: type.running,
      total: type.total,
      spend: cost === undefined ? null : {turns: 0, unpricedTurns: 0, usage: NOTHING_YET},
    }));
  return [...priced, ...rest];
}
