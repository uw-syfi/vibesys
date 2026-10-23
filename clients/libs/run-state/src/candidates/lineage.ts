/**
 * The ideas as a tree of what each built on, with what each subtree holds: a pure fold of a
 * candidate tree (`Candidate.parent` links; null is the input), computed in one pass over the
 * candidates because a parent always comes before its children.
 */
import type {Candidate, CandidateTree} from './types.js';

/** What the ideas at and below one idea hold. */
export interface SubtreeStats {
  /** Ideas in the subtree, the idea itself included. */
  readonly size: number;
  readonly passed: number;
  readonly failed: number;
  readonly running: number;
}

export interface Lineage {
  /** The ideas that built on the input, oldest first. */
  readonly roots: readonly string[];
  /** The ideas that built on each idea, oldest first; an idea nothing built on has no entry. */
  readonly children: ReadonlyMap<string, readonly string[]>;
  /** Steps from the input: an idea built on the input is 1. */
  readonly depth: ReadonlyMap<string, number>;
  readonly subtree: ReadonlyMap<string, SubtreeStats>;
}

function merged(item: Candidate, below: readonly SubtreeStats[]): SubtreeStats {
  let size = 1;
  let passed = item.outcome === 'passed' ? 1 : 0;
  let failed = item.outcome === 'failed' ? 1 : 0;
  let running = item.outcome === 'running' ? 1 : 0;
  for (const stats of below) {
    size += stats.size;
    passed += stats.passed;
    failed += stats.failed;
    running += stats.running;
  }
  return {size, passed, failed, running};
}

const memo = new WeakMap<CandidateTree, Lineage>();

/** The lineage of `tree`; the same object while the tree is unchanged. */
export function candidateLineage(tree: CandidateTree): Lineage {
  const known = memo.get(tree);
  if (known !== undefined) return known;
  const built = build(tree.candidates);
  memo.set(tree, built);
  return built;
}

function build(candidates: readonly Candidate[]): Lineage {
  const present = new Set(candidates.map(item => item.id));
  const roots: string[] = [];
  const children = new Map<string, string[]>();
  const depth = new Map<string, number>();
  for (const item of candidates) {
    // A base the tree does not hold is no base: the idea is read as built on the input.
    const base = item.parent !== null && present.has(item.parent) ? item.parent : null;
    if (base === null) {
      roots.push(item.id);
      depth.set(item.id, 1);
      continue;
    }
    const siblings = children.get(base);
    if (siblings === undefined) children.set(base, [item.id]);
    else siblings.push(item.id);
    depth.set(item.id, (depth.get(base) ?? 0) + 1);
  }
  const subtree = new Map<string, SubtreeStats>();
  for (let at = candidates.length - 1; at >= 0; at -= 1) {
    const item = candidates[at] as Candidate;
    const below = (children.get(item.id) ?? []).map(id => subtree.get(id) as SubtreeStats);
    subtree.set(item.id, merged(item, below));
  }
  return {roots, children, depth, subtree};
}
