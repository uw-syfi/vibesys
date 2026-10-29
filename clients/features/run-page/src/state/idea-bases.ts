/**
 * Where each idea started from, in the words the Ideas list and the idea view use: `best` (the
 * best passed idea when it started), `input`, another passed idea, or its own
 * work (a continuation of the round that already holds its code). Read from the base each
 * attempt recorded (`base_round`, the idea it built on) and from which idea was best when the
 * idea began, so a base that was the leader then still reads `best` after a better idea passed.
 *
 * Pure over the candidates; memoized on the tree and direction.
 */
import type {Candidate, CandidateTree} from '@vibesys/run-state/candidates';
import type {Direction} from './direction.js';

/** What an idea built on. */
export interface IdeaBase {
  readonly kind: 'best' | 'input' | 'idea' | 'own work';
  /** `best`, `input`, `own work`, `a passed idea` or `another idea`: short, whatever the title. */
  readonly label: string;
  /** The idea it built on, when that is another idea (`best` included); null for the input. */
  readonly parent: string | null;
}

export type IdeaBases = ReadonlyMap<string, IdeaBase>;

function beats(value: number, than: number, direction: Direction): boolean {
  return direction === 'min' ? value < than : value > than;
}

/** An idea whose own work the round continues: it names its own round as its base. */
function continuesItself(item: Candidate): boolean {
  return item.baseRound !== null && item.baseRound === item.round;
}

/** For each idea, the best passed idea that had finished when it started (null: none had). */
function bestWhenStarted(
  candidates: readonly Candidate[],
  direction: Direction,
): ReadonlyMap<string, string | null> {
  const finished = candidates
    .filter(
      item =>
        item.outcome === 'passed' && item.endedAt !== null && item.measurement?.metricValue != null,
    )
    .sort((a, b) => (a.endedAt ?? 0) - (b.endedAt ?? 0));
  const starts = [...candidates].sort((a, b) => a.startedAt - b.startedAt);
  const found = new Map<string, string | null>();
  let best: Candidate | null = null;
  let next = 0;
  for (const item of starts) {
    while (next < finished.length && (finished[next]?.endedAt ?? 0) <= item.startedAt) {
      const done = finished[next] as Candidate;
      const value = done.measurement?.metricValue ?? 0;
      const standing = best?.measurement?.metricValue ?? null;
      if (standing === null || beats(value, standing, direction)) best = done;
      next += 1;
    }
    found.set(item.id, best?.id ?? null);
  }
  return found;
}

function fold(tree: CandidateTree, direction: Direction): IdeaBases {
  const byId = new Map(tree.candidates.map(item => [item.id, item]));
  const leaders = bestWhenStarted(tree.candidates, direction);
  const bases = new Map<string, IdeaBase>();
  for (const item of tree.candidates) {
    const parent = item.parent === null ? null : (byId.get(item.parent) ?? null);
    if (continuesItself(item))
      bases.set(item.id, {kind: 'own work', label: 'own work', parent: null});
    else if (parent === null) bases.set(item.id, {kind: 'input', label: 'input', parent: null});
    else if (leaders.get(item.id) === parent.id) {
      bases.set(item.id, {kind: 'best', label: 'best', parent: parent.id});
    } else {
      const label = parent.outcome === 'passed' ? 'a passed idea' : 'another idea';
      bases.set(item.id, {kind: 'idea', label, parent: parent.id});
    }
  }
  return bases;
}

const memo = new WeakMap<CandidateTree, {direction: Direction; bases: IdeaBases}>();

/** The base of each idea of `tree`, by id; the same map for the same tree and direction. */
export function ideaBases(tree: CandidateTree, direction: Direction): IdeaBases {
  const known = memo.get(tree);
  if (known !== undefined && known.direction === direction) return known.bases;
  const bases = fold(tree, direction);
  memo.set(tree, {direction, bases});
  return bases;
}
