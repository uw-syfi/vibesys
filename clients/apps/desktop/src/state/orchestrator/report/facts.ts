/**
 * What the report's facts line says of the whole run: how much was measured and when last, how many
 * ideas, how many agents are working, and what it has cost. Every number is the host's (the search
 * view's series and ideas, the summary's agents, the cost view's totals) or a count of what the host
 * served; nothing is worked out from the clock. A count of what a truncated search view served is a
 * floor, so it says so (`atLeast`): the host does not serve how many measurements it left out.
 */
import type {SearchState} from '@vibesys/orchestrator-proto';
import type {CostView} from '../cost.js';
import {ms} from '../time.js';
import type {AgentsStrip} from './agents-strip.js';
import type {IdeasReport} from './ideas.js';

export interface RunFacts {
  readonly measured: {
    /** The measure jobs of the search view, each counted once, whatever number of objectives it measured. */
    readonly count: number;
    /** The host cut the search view short, so there were at least this many. */
    readonly atLeast: boolean;
    /** When the newest of them was recorded; null with none. */
    readonly latestMs: number | null;
  };
  /** The ideas of this run that the host served; null before any search view has been served. */
  readonly ideas: {readonly count: number; readonly atLeast: boolean} | null;
  readonly agents: {readonly running: number; readonly total: number};
  /** What the run has cost, as the cost view served it; undefined until it has been read. */
  readonly spend:
    | {
        /** The cost of the priced turns; null when no turn is priced. */
        readonly usd: number | null;
        /** Turns without a price are behind the figure, so it is a floor. */
        readonly floor: boolean;
        readonly turns: number;
        readonly unpricedTurns: number;
      }
    | undefined;
}

/** The measure jobs a search view serves and when the newest was recorded. */
function measuredOf(search: SearchState | undefined): RunFacts['measured'] {
  const jobs = new Set<string>();
  let latestMs: number | null = null;
  for (const series of search?.series ?? []) {
    for (const point of series.points) {
      jobs.add(point.jobId);
      const at = ms(point.at);
      if (at !== null && (latestMs === null || at > latestMs)) latestMs = at;
    }
  }
  return {count: jobs.size, atLeast: search?.truncated ?? false, latestMs};
}

/** The facts of a run's report. */
export function runFacts(
  search: SearchState | undefined,
  ideas: IdeasReport | undefined,
  agents: AgentsStrip,
  cost: CostView | undefined,
): RunFacts {
  return {
    measured: measuredOf(search),
    ideas:
      ideas === undefined
        ? null
        : {count: ideas.ideas.filter(idea => idea.inRun).length, atLeast: ideas.truncated},
    agents: {running: agents.running, total: agents.total},
    spend:
      cost === undefined
        ? undefined
        : {
            usd: cost.total.costUsd,
            floor: cost.total.costUsd !== null && cost.unpricedTurns > 0,
            turns: cost.turns,
            unpricedTurns: cost.unpricedTurns,
          },
  };
}
