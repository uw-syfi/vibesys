/**
 * The best idea while the run works. The run's adoption span, which names the best, opens when the
 * run ends (core adopts the last choice then), so until it does the candidate tree has no best and
 * the hero and the Ideas list would read as if nothing had passed. `withLiveBest` fills the gap
 * with the same reading the performance chart draws as its step line: the latest idea that passed
 * and beat the input and every passed idea before it (`performanceFold`). The same holds for an
 * adoption span that says the input stands while the run still works: a search that has not named
 * its leader yet. Once the span names a candidate, or the run decided, it is the best, and this
 * changes nothing.
 */
import type {CandidateTree} from '@vibesys/run-state/candidates';
import type {Direction} from './direction.js';
import {inputValueOf, performanceFold} from './performance.js';

/** The candidates from the input to `best`, each built on the one before. */
function pathTo(tree: CandidateTree, best: string): string[] {
  const byId = new Map(tree.candidates.map(item => [item.id, item]));
  const path: string[] = [];
  for (let at = byId.get(best); at !== undefined; at = byId.get(at.parent ?? '')) path.push(at.id);
  return path.reverse();
}

/** An adoption span that says the input stands and has not been decided: the search named no leader yet. */
function standingUndecided(tree: CandidateTree): boolean {
  return tree.best !== null && tree.best.candidate === null && !tree.best.adopted;
}

const MEMO = new WeakMap<CandidateTree, {direction: Direction; tree: CandidateTree}>();

/**
 * `tree` with the best so far as its best when the run has not opened its adoption span or it
 * names no leader yet; the same
 * object for the same tree and direction, so a reader's caches keep working. The input standing
 * (nothing passed and beat it) leaves the best empty, as the adoption span does.
 */
export function withLiveBest(tree: CandidateTree, direction: Direction): CandidateTree {
  if (tree.adoptionOpened && !standingUndecided(tree)) return tree;
  const memo = MEMO.get(tree);
  if (memo !== undefined && memo.direction === direction) return memo.tree;
  const fold = performanceFold(tree.candidates, direction, inputValueOf(tree.candidates));
  const leader = fold.points.filter(point => point.best).at(-1);
  const live: CandidateTree =
    leader === undefined
      ? tree
      : {
          ...tree,
          best: {candidate: leader.id, adopted: false},
          bestPath: pathTo(tree, leader.id),
        };
  MEMO.set(tree, {direction, tree: live});
  return live;
}
