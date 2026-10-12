import type {
  IterationPage,
  IterationResult,
  IterationSummary,
  IterationUnavailable,
} from '@vibesys/contracts';
import {roundsWithPlan} from './round-plan.js';
import type {CoreState} from './state.js';

export interface IterationNavigationState {
  readonly runId: string | null;
  readonly revision: string | null;
  readonly unavailable: IterationUnavailable | null;
  readonly byId: Readonly<Record<string, IterationSummary>>;
  readonly childIds: Readonly<Record<string, readonly string[]>>;
  readonly nextCursor: Readonly<Record<string, string | null>>;
  readonly appliedPages: Readonly<Record<string, Readonly<Record<string, string>>>>;
}

const ROOT = '';
const FIRST_PAGE = '<first>';

export function initialIterationNavigation(): IterationNavigationState {
  return {
    runId: null,
    revision: null,
    unavailable: null,
    byId: {},
    childIds: {},
    nextCursor: {},
    appliedPages: {},
  };
}

/** Apply one authoritative page. A null `from_cursor` replaces that sibling set. */
export function reduceIterationResult(
  state: IterationNavigationState,
  result: IterationResult,
): IterationNavigationState {
  if (isUnavailable(result)) {
    return {...initialIterationNavigation(), unavailable: result};
  }
  const base = compatibleBase(state, result);
  const key = result.parent_id ?? ROOT;
  const fromCursor = result.from_cursor ?? null;
  const pageKey = fromCursor ?? FIRST_PAGE;
  const signature = pageSignature(result);
  const replay = replayedPage(base, result, key, pageKey, signature);
  if (replay !== null) return replay;
  const priorIds = fromCursor === null ? [] : (base.childIds[key] ?? []);
  if (fromCursor !== null && base.nextCursor[key] !== fromCursor) {
    throw new Error(
      `iteration page cursor does not continue parent ${result.parent_id ?? '<root>'}`,
    );
  }
  const {ids, byId} = mergePage(base, priorIds, result);
  return {
    runId: result.run_id,
    revision: result.projection_revision,
    unavailable: null,
    byId,
    childIds: {...base.childIds, [key]: ids},
    nextCursor: {...base.nextCursor, [key]: result.next_cursor ?? null},
    appliedPages: {
      ...base.appliedPages,
      [key]: {...base.appliedPages[key], [pageKey]: signature},
    },
  };
}

function replayedPage(
  state: IterationNavigationState,
  page: IterationPage,
  parentKey: string,
  pageKey: string,
  signature: string,
): IterationNavigationState | null {
  const priorSignature = state.appliedPages[parentKey]?.[pageKey];
  if (priorSignature === undefined) return null;
  if (priorSignature === signature) return state;
  throw new Error(`iteration page replay conflicts for parent ${page.parent_id ?? '<root>'}`);
}

function mergePage(
  state: IterationNavigationState,
  priorIds: readonly string[],
  page: IterationPage,
): {ids: string[]; byId: Record<string, IterationSummary>} {
  const ids = [...priorIds];
  const byId = {...state.byId};
  for (const item of page.items) {
    if (!ids.includes(item.id)) ids.push(item.id);
    byId[item.id] = item;
  }
  return {ids, byId};
}

function pageSignature(page: IterationPage): string {
  return JSON.stringify({items: page.items, nextCursor: page.next_cursor ?? null});
}

function isUnavailable(result: IterationResult): result is IterationUnavailable {
  return result.kind === 'unavailable' || !('items' in result);
}

function compatibleBase(
  state: IterationNavigationState,
  page: IterationPage,
): IterationNavigationState {
  if (state.runId === page.run_id && state.revision === page.projection_revision) return state;
  if ((page.from_cursor ?? null) !== null) {
    throw new Error('iteration continuation belongs to a different projection revision');
  }
  return initialIterationNavigation();
}

export function iterationChildren(
  state: IterationNavigationState,
  parentId: string | null,
): readonly IterationSummary[] {
  return (state.childIds[parentId ?? ROOT] ?? []).flatMap(id => {
    const item = state.byId[id];
    return item === undefined ? [] : [item];
  });
}

/** Compatibility projection for clients while policies migrate away from round-only navigation. */
export function iterationRootsForCore(state: CoreState): readonly IterationSummary[] {
  return iterationRootsForRounds(roundsWithPlan(state.rounds, state.maxRounds));
}

function iterationRootsForRounds(rounds: CoreState['rounds']): readonly IterationSummary[] {
  return rounds.map((round, index) => ({
    id:
      round.key.kind === 'number' ? `round:${round.key.number}` : `round-label:${round.key.label}`,
    parent_id: null,
    kind: 'round',
    label: round.number === null ? `Round ${index + 1}` : `Round ${round.number}`,
    ordinal: round.number ?? index,
    status:
      round.status === 'active'
        ? 'active'
        : round.status === 'failed'
          ? 'failed'
          : round.status === 'planned'
            ? 'pending'
            : 'completed',
    child_count: 0,
    headline_metrics: [],
  }));
}

/** Prefer queried policy navigation, retaining round navigation for old or unavailable servers. */
export function iterationRoots(
  navigation: IterationNavigationState,
  state: CoreState,
): readonly IterationSummary[] {
  const compatibility = iterationRootsForCore(state);
  if (navigation.revision === null) return compatibility;
  const queried = iterationChildren(navigation, null);
  // An authoritative empty projection may be non-round. Retain only rounds proven by events;
  // configured future rounds are compatibility data and must not invent policy semantics.
  if (queried.length === 0) return iterationRootsForRounds(state.rounds);
  if (!queried.every(node => node.kind === 'round')) return queried;
  const queriedIds = new Set(queried.map(node => node.id));
  return [...queried, ...compatibility.filter(node => !queriedIds.has(node.id))].sort(
    (left, right) => left.ordinal - right.ordinal || left.id.localeCompare(right.id),
  );
}
