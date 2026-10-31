/**
 * The Ideas face as a tree: each idea nested under the idea it built on, as the flat rows the list
 * draws. It stays readable at hundreds of ideas by showing only what leads somewhere:
 *
 * - the best idea's chain from the input is always open (its last `CHAIN_TAIL` ancestors, under a
 *   `… N earlier` row for the rest) and its rows carry the accent rail;
 * - every other idea that others built on is one row that says what is below it (`→ 7 ideas · best
 *   1.42× · 2 failed`) until opened;
 * - an opened idea that only one idea built on, repeatedly, shows its first and last and a
 *   `… N ideas …` row for the ones between, when they are `COMPRESS_FROM` or more;
 * - children come newest first; the guides stop growing at `INDENT_CAP` levels and the row's
 *   lineage depth is written instead;
 * - a search shows the ideas that match and the ideas they build on, all open.
 *
 * Opening is the page's state (`open`: idea ids, `run:<id>` for a compressed run, `earlier`), and
 * the idea the reader pinned (the header line, a pick) has its ancestors open. Nothing here depends
 * on the pointer: highlighting an idea's ancestry (`ideaAncestry`) is the screen's job.
 */
import {candidateLineage, type Lineage} from '@vibesys/run-state/candidates';
import {objectiveMetric} from './baselines.js';
import {type IdeaRow, type IdeasFilter, ideaRowsById, ideasView} from './ideas-view.js';
import type {RunModel} from './run-model.js';
import {ratio} from './span-reading.js';

/** The two layouts of the Ideas face. */
export type IdeasLayout = 'list' | 'tree';

/** How many of the best chain's last ancestors show before the `… N earlier` row. */
export const CHAIN_TAIL = 5;
/** How many ideas a one-child run must hold between its ends to fold into `… N ideas …`. */
export const COMPRESS_FROM = 3;
/** The levels of guides drawn; deeper rows write their depth instead. */
export const INDENT_CAP = 8;
/** The cells a capped row keeps after the dotted one: its own branch and its parent's, beside the depth number. */
const CAPPED_CELLS = 2;
/** The key in `open` of the best chain's `… N earlier` row. */
export const EARLIER = 'earlier';

type TreeGuide = 'pipe' | 'tee' | 'elbow' | 'gap' | 'blank';

interface IdeaTreeRow {
  readonly kind: 'idea';
  readonly row: IdeaRow;
  readonly guides: readonly TreeGuide[];
  /** Steps from the input, written in place of guides past `INDENT_CAP`; else null. */
  readonly depth: number | null;
  /**
   * Ideas below it: `closed` is one row standing for them (`summary` says what they hold), `open`
   * shows them, `held` is open and stays so, and `none` has none to show.
   */
  readonly below: 'none' | 'held' | 'open' | 'closed';
  /** What the closed subtree holds, worded; null unless `below` is `closed`. */
  readonly summary: string | null;
}

/** A row that stands for ideas left out: `… 7 ideas …`, `… 9 earlier`. */
interface FoldTreeRow {
  readonly kind: 'fold';
  /** The key in `open` that shows them. */
  readonly key: string;
  readonly label: string;
  readonly guides: readonly TreeGuide[];
}

export type IdeaTreeEntry = IdeaTreeRow | FoldTreeRow;

export interface IdeaTree {
  readonly rows: readonly IdeaTreeEntry[];
  readonly counts: ReturnType<typeof ideasView>['counts'];
  readonly total: number;
}

export interface TreeShape {
  readonly filter: IdeasFilter;
  /** What the reader opened. */
  readonly open: ReadonlySet<string>;
  /** The idea whose ancestors stay open; null for none. */
  readonly pinned: string | null;
}

/** The layout the Ideas face starts in: a tree once any idea builds on another, else the list. */
export function defaultLayout(model: RunModel): IdeasLayout {
  return candidateLineage(model.candidates).children.size > 0 ? 'tree' : 'list';
}

/** `id` and the ideas it builds on back to the input; empty for an id that is no idea. */
export function ideaAncestry(model: RunModel, id: string | null): ReadonlySet<string> {
  const byId = new Map(model.candidates.candidates.map(item => [item.id, item]));
  const chain = new Set<string>();
  let at = id === null ? undefined : byId.get(id);
  while (at !== undefined && !chain.has(at.id)) {
    chain.add(at.id);
    at = at.parent === null ? undefined : byId.get(at.parent);
  }
  return chain;
}

/** The header line over the Ideas face: the best idea and how far it is from the input. */
export interface BestLine {
  readonly id: string;
  readonly title: string;
  /** Ideas from the input to it, itself included. */
  readonly steps: number;
}

export function bestLine(model: RunModel): BestLine | null {
  const {bestPath, candidates} = model.candidates;
  const id = bestPath.at(-1);
  const best = candidates.find(item => item.id === id);
  return id === undefined || best === undefined
    ? null
    : {id, title: best.title, steps: bestPath.length};
}

/**
 * The ratio of the latest idea core marked a new best below each idea, at any depth. Core's new
 * bests only improve, so the latest one in a subtree is its best; the client compares nothing.
 */
function newBestBelow(model: RunModel): ReadonlyMap<string, number> {
  const metric = objectiveMetric(model);
  const points = metric === null ? undefined : model.series?.get(metric)?.points;
  const items = new Map(model.candidates.candidates.map(item => [item.id, item]));
  const below = new Map<string, number>();
  for (const point of points ?? []) {
    const found = point.best ? (items.get(point.id)?.measurement?.vsBaseline ?? null) : null;
    if (found === null) continue;
    const seen = new Set<string>();
    for (
      let at = items.get(point.id)?.parent ?? null;
      at !== null && !seen.has(at);
      at = items.get(at)?.parent ?? null
    ) {
      seen.add(at);
      below.set(at, found);
    }
  }
  return below;
}

/** What the ideas built on `id`, at any depth, hold: `→ 7 ideas · best 1.42× · 2 failed`. */
function summaryOf(walk: Walk, id: string): string {
  let size = 0;
  let failed = 0;
  for (const kid of walk.lineage.children.get(id) ?? []) {
    const stats = walk.lineage.subtree.get(kid);
    if (stats === undefined) continue;
    size += stats.size;
    failed += stats.failed;
  }
  const best = walk.newBest.get(id);
  const parts = [`→ ${size} ${size === 1 ? 'idea' : 'ideas'}`];
  if (best !== undefined) parts.push(`best ${ratio(best)}`);
  if (failed > 0) parts.push(`${failed} failed`);
  return parts.join(' · ');
}

/** The ideas to draw: those the filter shows and those they build on. */
function visibleIdeas(model: RunModel, shown: ReadonlySet<string>): Set<string> {
  const visible = new Set(shown);
  const items = model.candidates.candidates;
  // A parent comes before its children, so one pass from the end reaches every ancestor.
  for (let at = items.length - 1; at >= 0; at -= 1) {
    const item = items[at];
    if (item !== undefined && item.parent !== null && visible.has(item.id))
      visible.add(item.parent);
  }
  return visible;
}

interface Walk {
  readonly lineage: Lineage;
  readonly parents: ReadonlyMap<string, string | null>;
  /** The ratio of core's latest new best below each idea. */
  readonly newBest: ReadonlyMap<string, number>;
  readonly rows: ReadonlyMap<string, IdeaRow>;
  readonly visible: ReadonlySet<string>;
  readonly shape: TreeShape;
  readonly searching: boolean;
  /** The best chain, input first, or empty when a filter hides part of it. */
  readonly chain: readonly string[];
  readonly onChain: ReadonlySet<string>;
  /** The ideas above the pinned one. */
  readonly held: ReadonlySet<string>;
  readonly out: IdeaTreeEntry[];
}

/** An idea's shown children, newest first. */
function kidsOf(walk: Walk, id: string): readonly string[] {
  const kids = walk.lineage.children.get(id) ?? [];
  const shown: string[] = [];
  for (let at = kids.length - 1; at >= 0; at -= 1) {
    const kid = kids[at] as string;
    if (walk.visible.has(kid)) shown.push(kid);
  }
  return shown;
}

/** Where a row's children's guides start: one cell per level above, a branch or its end last. */
function below(more: readonly boolean[], last: boolean | null): TreeGuide[] {
  const cells = more.map(open => (open ? ('pipe' as const) : ('blank' as const)));
  return last === null ? cells : [...cells, last ? ('elbow' as const) : ('tee' as const)];
}

function drawn(guides: readonly TreeGuide[]): {guides: TreeGuide[]; capped: boolean} {
  const capped = guides.length > INDENT_CAP;
  // The levels left out are one dotted cell, so a deep row still sits visibly deep.
  return {guides: capped ? ['gap', ...guides.slice(-CAPPED_CELLS)] : [...guides], capped};
}

function pushFold(walk: Walk, key: string, label: string, guides: readonly TreeGuide[]): void {
  walk.out.push({kind: 'fold', key, label, guides: drawn(guides).guides});
}

/** `siblings` at one level; `more` is, per level above, whether that level's branch goes on. */
function siblings(walk: Walk, ids: readonly string[], more: readonly boolean[], top: boolean) {
  ids.forEach((id, index) => {
    const last = index === ids.length - 1;
    place(walk, id, {
      guides: top ? [] : below(more, last),
      more: top ? [] : [...more, !last],
      only: false,
    });
  });
}

interface Spot {
  readonly guides: readonly TreeGuide[];
  /** What the children's guides continue: one entry per level above them. */
  readonly more: readonly boolean[];
  /** The only child of an open idea: it opens with it. */
  readonly only: boolean;
  /** In a one-child run the reader opened: its rest does not fold again. */
  readonly flat?: boolean;
}

function place(walk: Walk, id: string, spot: Spot): void {
  const row = walk.rows.get(id);
  if (row === undefined) return;
  const kids = kidsOf(walk, id);
  const held = walk.searching || walk.onChain.has(id) || walk.held.has(id);
  const opened = held || spot.only || walk.shape.open.has(id);
  let state: IdeaTreeRow['below'] = 'none';
  if (kids.length > 0) state = held ? 'held' : opened ? 'open' : 'closed';
  const {guides, capped} = drawn(spot.guides);
  walk.out.push({
    kind: 'idea',
    row,
    guides,
    depth: capped ? (walk.lineage.depth.get(id) ?? null) : null,
    below: state,
    summary: state === 'closed' ? summaryOf(walk, id) : null,
  });
  if (state === 'closed' || state === 'none') return;
  children(walk, kids, spot.more, spot.flat === true);
}

/** The ideas an open idea's children: folded when they are one idea after another. */
function children(
  walk: Walk,
  kids: readonly string[],
  more: readonly boolean[],
  flat: boolean,
): void {
  const first = kids[0];
  if (kids.length !== 1 || first === undefined || walk.searching) {
    siblings(walk, kids, more, false);
    return;
  }
  if (!flat) {
    const run: string[] = [];
    let end = first;
    for (let next = kidsOf(walk, end); next.length === 1; next = kidsOf(walk, end)) {
      if (walk.onChain.has(end) || walk.held.has(end)) break;
      run.push(end);
      end = next[0] as string;
    }
    const key = `run:${first}`;
    if (run.length >= COMPRESS_FROM && !walk.shape.open.has(key)) {
      pushFold(walk, key, `… ${run.length} ideas …`, below(more, true));
      place(walk, end, {
        guides: below([...more, false], true),
        more: [...more, false, false],
        only: true,
        flat: true,
      });
      return;
    }
  }
  place(walk, first, {
    guides: below(more, true),
    more: [...more, false],
    only: true,
    flat: true,
  });
}

/**
 * Whether the pinned idea sits on a branch that leaves the chain inside `hidden`: only then must
 * the earlier ancestors show. A pinned idea on the chain's tail (or the best itself) hides them.
 */
function opensHidden(walk: Walk, hidden: ReadonlySet<string>): boolean {
  for (const id of [...walk.held, walk.shape.pinned ?? '']) {
    if (walk.onChain.has(id)) continue;
    const parent = walk.parents.get(id);
    if (parent !== undefined && parent !== null && hidden.has(parent)) return true;
  }
  return false;
}

/** The best chain's rows: the last `CHAIN_TAIL` ancestors and itself, under `… N earlier`. */
function chained(walk: Walk, roots: readonly string[]): boolean {
  const {chain} = walk;
  const hidden = chain.length - (CHAIN_TAIL + 1);
  const top = chain[0];
  if (top === undefined || hidden < 2 || walk.shape.open.has(EARLIER)) return false;
  if (opensHidden(walk, new Set(chain.slice(0, hidden)))) return false;
  // The roots stand as siblings (newest first), the chain's own at its place with `… N earlier`.
  for (const id of roots) {
    if (id !== top) {
      place(walk, id, {guides: [], more: [], only: false});
      continue;
    }
    pushFold(walk, EARLIER, `… ${hidden} earlier`, []);
    const tail = chain[hidden];
    if (tail !== undefined) place(walk, tail, {guides: ['elbow'], more: [false], only: true});
  }
  return true;
}

export function ideaTree(model: RunModel, shape: TreeShape): IdeaTree {
  const view = ideasView(model, shape.filter);
  const lineage = candidateLineage(model.candidates);
  const visible = visibleIdeas(model, new Set(view.rows.map(row => row.id)));
  const searching = shape.filter.query.trim() !== '';
  const path = model.candidates.bestPath;
  const chain = searching || !path.every(id => visible.has(id)) ? [] : path;
  const held = new Set<string>();
  for (const id of ideaAncestry(model, shape.pinned)) if (id !== shape.pinned) held.add(id);
  const walk: Walk = {
    lineage,
    parents: new Map(model.candidates.candidates.map(item => [item.id, item.parent])),
    newBest: newBestBelow(model),
    rows: ideaRowsById(model),
    visible,
    shape,
    searching,
    chain,
    onChain: new Set(chain),
    held,
    out: [],
  };
  const roots = kidsRoots(walk);
  if (!chained(walk, roots)) siblings(walk, roots, [], true);
  return {rows: walk.out, counts: view.counts, total: view.total};
}

function kidsRoots(walk: Walk): string[] {
  const roots = walk.lineage.roots;
  const shown: string[] = [];
  for (let at = roots.length - 1; at >= 0; at -= 1) {
    const id = roots[at] as string;
    if (walk.visible.has(id)) shown.push(id);
  }
  return shown;
}
