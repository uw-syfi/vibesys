/**
 * What a project's runs spent in a window of time (`usage.window`, feature `usage_window`) in the
 * app's own terms: the figures the Usage page shows, with times in milliseconds. The helper sums
 * the runs' usage records, so nothing here derives a cost.
 */
import type {TokenTally, UsageWindowReply} from '@vibesys/contracts/host-helper';

/** The most runs one question names; ask in pieces beyond it. */
export const USAGE_WINDOW_MAX_RUNS = 100;
/** The longest window the helper answers, in hours (two weeks). */
export const USAGE_WINDOW_MAX_HOURS = 336;

const MS_PER_HOUR = 3_600_000;

/** What one run spent in the window; a run with no turn that ended in it is not listed. */
export interface RunSpend {
  readonly runId: string;
  /** The task the run was started for; null when the run records none. */
  readonly task: string | null;
  readonly costUsd: number;
  /** Its turns in the window that reported no cost. */
  readonly unpricedTurns: number;
}

/** What one kind of agent spent: a role on a harness and model, each null where unrecorded. */
export interface AgentSpend {
  readonly role: string | null;
  readonly harness: string | null;
  readonly model: string | null;
  readonly costUsd: number;
  readonly turns: number;
  /** Those of `turns` that reported no cost. */
  readonly unpricedTurns: number;
}

/** The Usage page's figures for the window `[since, until)`. */
export interface UsageWindowEntry {
  /** The window, in milliseconds. `until` is "now" for the rate: the client names it. */
  readonly since: number;
  readonly until: number;
  /** A lower bound while `unpricedTurns` is above zero: a turn that reported no cost adds none. */
  readonly spentUsd: number;
  /** The cost of the last hour of the window (all of a shorter window) per hour. */
  readonly spendPerHourUsd: number;
  /** A count the provider did not report is null, not zero. */
  readonly tokens: TokenTally;
  readonly turns: number;
  readonly unpricedTurns: number;
  /**
   * Whether the helper said how many unpriced turns each run and each agent holds. A helper that
   * predates the per-row counts sends none, so `unpricedTurns` has no breakdown to explain it.
   */
  readonly unpricedItemised: boolean;
  /** Turns with no recorded end time: they belong to no window. */
  readonly untimedTurns: number;
  /** Ideas kept as the best so far whose result landed in the window. */
  readonly newBests: number;
  /** `spentUsd` per new best; null when none landed. */
  readonly costPerNewBestUsd: number | null;
  /** The costliest run first. */
  readonly byRun: readonly RunSpend[];
  /** The costliest agent first, at most 64. */
  readonly byAgent: readonly AgentSpend[];
  /** The agents that spent in the window beyond those in `byAgent`. */
  readonly agentsOmitted: number;
  /** The runs asked for that this host's project does not have or holds no journal of. */
  readonly missingRuns: readonly string[];
}

/** What a host answers a usage question: figures, or that the host's VibeSys offers none. */
export type UsageWindowOutcome =
  | {readonly kind: 'usage'; readonly usage: UsageWindowEntry}
  | {readonly kind: 'unsupported'};

/**
 * Why a window or run list cannot be asked of the helper, as words; null when it can. The helper
 * refuses the same questions, so asking one is a mistake of the caller, not of the host.
 */
export function usageWindowRefusal(
  runIds: readonly string[],
  since: number,
  until: number,
): string | null {
  if (!Number.isFinite(since) || !Number.isFinite(until) || since >= until) {
    return 'the usage window must end after it begins';
  }
  if (until - since > USAGE_WINDOW_MAX_HOURS * MS_PER_HOUR) {
    return `the usage window may span at most ${USAGE_WINDOW_MAX_HOURS} hours`;
  }
  if (runIds.length > USAGE_WINDOW_MAX_RUNS) {
    return `a usage question names at most ${USAGE_WINDOW_MAX_RUNS} runs`;
  }
  return null;
}

/** The helper's `usage.window` reply in the app's terms. */
export function usageWindowEntry(reply: UsageWindowReply): UsageWindowEntry {
  return {
    since: Date.parse(reply.since),
    until: Date.parse(reply.until),
    spentUsd: reply.spent_usd,
    spendPerHourUsd: reply.spend_per_hour_usd,
    tokens: reply.tokens,
    turns: reply.turns,
    unpricedTurns: reply.unpriced_turns,
    unpricedItemised:
      reply.by_run.every(run => run.unpriced_turns !== undefined) &&
      reply.by_agent.every(agent => agent.unpriced_turns !== undefined),
    untimedTurns: reply.untimed_turns,
    newBests: reply.new_bests,
    costPerNewBestUsd: reply.cost_per_new_best_usd,
    // A helper that predates the per-run and per-agent counts sends none: zero is "not said".
    byRun: reply.by_run.map(({run_id, task, cost_usd, unpriced_turns}) => ({
      runId: run_id,
      task,
      costUsd: cost_usd,
      unpricedTurns: unpriced_turns ?? 0,
    })),
    byAgent: reply.by_agent.map(({role, harness, model, cost_usd, turns, unpriced_turns}) => ({
      role,
      harness,
      model,
      costUsd: cost_usd,
      turns,
      unpricedTurns: unpriced_turns ?? 0,
    })),
    agentsOmitted: reply.agents_omitted,
    missingRuns: [...reply.missing_runs],
  };
}
