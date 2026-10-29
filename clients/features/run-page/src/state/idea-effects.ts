/**
 * Whether each idea's effect held, as the framework judged it: the verdict of the comparison of
 * the idea's measurement with its base's (`measurement_compared`), worded `supported`, `within
 * noise` or `disproven`. It sits next to passed and failed so a screen never says an idea passed
 * while the run's own history calls its effect inconclusive. The comparison is of the idea's own
 * measured code with its base's, so only an attempt that measured a value counts: a failed attempt
 * that kept its base's revision would otherwise borrow the verdict of the idea that made that
 * revision. While the idea still runs its comparison keeps taking in runs (the first is one
 * against one), so the verdict is `measuring` until the idea ends. An idea with no comparison
 * (not measured, or the backend has not emitted one) has none.
 *
 * Pure over the core's comparisons and the candidates; memoized on both.
 */
import type {CoreState} from '@vibesys/run-state';
import type {CandidateTree} from '@vibesys/run-state/candidates';

export type IdeaEffect = 'supported' | 'within noise' | 'disproven' | 'inconclusive' | 'measuring';

/**
 * The verdict as a screen words it. It is against the idea's base, not the input, so it says so:
 * `within noise` beside an idea's 6.72x would read as a claim about the input.
 */
export function effectWords(effect: IdeaEffect): string {
  switch (effect) {
    case 'supported':
      return 'beat its base';
    case 'within noise':
      return 'within noise of its base';
    case 'disproven':
      return 'did not beat its base';
    case 'inconclusive':
      return 'too few runs to tell';
    case 'measuring':
      return 'measuring';
  }
}

export type IdeaEffects = ReadonlyMap<string, IdeaEffect>;

const WORDS = {
  better: 'supported',
  within_noise: 'within noise',
  worse: 'disproven',
  inconclusive: 'inconclusive',
} as const satisfies Record<string, IdeaEffect>;

function fold(core: CoreState, tree: CandidateTree): IdeaEffects {
  const found = new Map<string, IdeaEffect>();
  for (const item of tree.candidates) {
    for (const attempt of [...item.attempts].reverse()) {
      if (attempt.measurement?.metricValue == null || attempt.revision === null) continue;
      const comparison = core.comparisons[attempt.revision];
      if (comparison === undefined) continue;
      found.set(item.id, item.outcome === 'running' ? 'measuring' : WORDS[comparison.verdict]);
      break;
    }
  }
  return found;
}

const NONE: IdeaEffects = new Map();
const memo = new WeakMap<CoreState['comparisons'], WeakMap<CandidateTree, IdeaEffects>>();

/** The effect verdict of each idea that has one, by idea id; empty before the run's fold. */
export function ideaEffects(core: CoreState | null, tree: CandidateTree): IdeaEffects {
  if (core === null) return NONE;
  const byTree = memo.get(core.comparisons) ?? new WeakMap<CandidateTree, IdeaEffects>();
  memo.set(core.comparisons, byTree);
  const cached = byTree.get(tree);
  if (cached !== undefined) return cached;
  const folded = fold(core, tree);
  byTree.set(tree, folded);
  return folded;
}
