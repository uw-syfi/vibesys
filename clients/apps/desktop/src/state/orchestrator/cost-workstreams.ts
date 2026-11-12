/**
 * The workstreams a Cost headline may claim from the run stream. SearchState already counts every
 * in-run idea by workstream; this selector counts those served groups and says whether the host has
 * proved that the count is whole. It never infers a workstream from an agent or a turn.
 */
import type {OrchestratorState} from './state.js';

/** A host's named run-workstreams: one workstream with an idea in one run counts once. */
export interface ExploredWorkstreams {
  readonly count: number;
  /** Every host run and every named workstream view is represented in `count`. */
  readonly complete: boolean;
}

const MAX_WORKSTREAM_VIEWS = 101;

/**
 * Counts the named workstream rows the host served. The zero row is ideas without a workstream and
 * is deliberately not called one. A SearchState below the row cap is whole for this fact even when
 * another bounded part of that state made `truncated` true; at the cap, only an untruncated state
 * proves there was no 102nd row.
 *
 * `expectedRuns` comes from the whole-history HostCost view. Matching it proves that the run and
 * cost streams are accounting for the same number of host runs. Without that independent count,
 * the served rows remain a useful lower bound but are not called complete.
 */
export function exploredWorkstreams(
  state: Pick<OrchestratorState, 'runs' | 'searches' | 'cut'>,
  expectedRuns: number | null,
): ExploredWorkstreams {
  const runIds = Object.keys(state.runs);
  let count = 0;
  let searchesComplete = true;
  for (const runId of runIds) {
    const search = state.searches[runId];
    if (search === undefined || search.health?.state.case !== 'liveSince') {
      searchesComplete = false;
      continue;
    }
    count += search.workstreamViews.filter(view => view.number > 0).length;
    if (search.truncated && search.workstreamViews.length === MAX_WORKSTREAM_VIEWS) {
      searchesComplete = false;
    }
  }
  return {
    count,
    complete:
      expectedRuns !== null &&
      expectedRuns === runIds.length &&
      state.cut.runsLeftOut === 0 &&
      searchesComplete,
  };
}
