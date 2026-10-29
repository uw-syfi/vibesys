/**
 * The agent tree of the Agents face as the flat rows the list draws: each agent once, under the
 * first of the agents that started it (`startedBy`) that the run knows, with the others as
 * `alsoUnder` for fan-in; agents nobody known started are roots. Children follow in the order
 * they started. Subtrees whose agents are all done fold into one `+ N finished` row once a parent
 * has `FOLD_FROM` of them (a few finished siblings read better in place); a subtree with any agent
 * not done stays open, and a fold in `expanded` (by its parent's key) shows its subtrees. The
 * `selected` agent and its ancestors are never folded: the group that holds them stays shown.
 *
 * Rows are a function of the graph, the spans (for each agent's latest headline) and `expanded`,
 * built in one pass over the agents; the list's windowing keeps a large run cheap to draw.
 */
import {roleName, type Span} from '@vibesys/contracts';
import type {AgentGraph, AgentNode} from '@vibesys/run-state/agents';

/** One lineage cell before a row, as the list draws it. */
type TreeGuide = 'pipe' | 'tee' | 'elbow' | 'blank';

/** How many finished sibling subtrees a parent needs before they fold. */
export const FOLD_FROM = 4;

/** The key of the roots' fold in `expanded`. */
export const ROOT_FOLD = '';

export interface AgentRow {
  readonly kind: 'agent';
  readonly agent: AgentNode;
  readonly guides: readonly TreeGuide[];
  /** The roles of the other parents the run knows, for `+ also under <role>`. */
  readonly alsoUnder: readonly string[];
  /** The latest turn's headline when it says more than the role. */
  readonly headline: string | null;
}

export interface FoldRow {
  readonly kind: 'fold';
  /** The parent's id (`ROOT_FOLD` for roots): what `expanded` holds once shown. */
  readonly parent: string;
  /** The agents folded away. */
  readonly count: number;
  readonly guides: readonly TreeGuide[];
}

export type TreeRow = AgentRow | FoldRow;

interface Placed {
  readonly agent: AgentNode;
  readonly children: Placed[];
  /** Agents in the subtree, itself included. */
  size: number;
  finished: boolean;
  /** Whether the selected agent is this one or below it. */
  holdsSelected: boolean;
}

/** Where each agent sits: under its first known parent; a ring of agents opens at its first. */
function place(agents: readonly AgentNode[]): {
  readonly roots: Placed[];
  readonly parentOf: (agent: AgentNode) => string | null;
} {
  const nodes = new Map<string, Placed>();
  for (const agent of agents)
    nodes.set(agent.id, {agent, children: [], size: 1, finished: false, holdsSelected: false});
  const parentOf = (agent: AgentNode): string | null =>
    agent.startedBy.find(id => id !== agent.id && nodes.has(id)) ?? null;
  const above = (node: Placed): Placed | undefined => {
    const parent = parentOf(node.agent);
    return parent === null ? undefined : nodes.get(parent);
  };
  const roots: Placed[] = [];
  for (const node of nodes.values()) {
    const parent = above(node);
    if (parent === undefined) roots.push(node);
    else parent.children.push(node);
  }
  const reached = new Set<Placed>();
  const reach = (node: Placed): void => {
    if (reached.has(node)) return;
    reached.add(node);
    node.children.forEach(reach);
  };
  roots.forEach(reach);
  for (const node of nodes.values()) {
    if (reached.has(node)) continue;
    const parent = above(node);
    parent?.children.splice(parent.children.indexOf(node), 1);
    roots.push(node);
    reach(node);
  }
  return {roots, parentOf};
}

/** Sets `size` and `finished` of each subtree, children first. */
function measure(node: Placed, selected: string | null): void {
  node.size = 1;
  node.finished = node.agent.status === 'done';
  node.holdsSelected = node.agent.id === selected;
  for (const child of node.children) {
    measure(child, selected);
    node.size += child.size;
    node.finished &&= child.finished;
    node.holdsSelected ||= child.holdsSelected;
  }
}

export function agentTree(
  graph: AgentGraph,
  spans: ReadonlyMap<string, Span>,
  expanded: ReadonlySet<string>,
  selected: string | null = null,
): readonly TreeRow[] {
  const {roots, parentOf} = place(graph.agents);
  const roles = new Map(graph.agents.map(agent => [agent.id, roleName(agent.role)]));
  for (const root of roots) measure(root, selected);
  const rows: TreeRow[] = [];

  const headlineOf = (agent: AgentNode): string | null => {
    const title = spans.get(agent.activity.turn)?.label?.title;
    return title === undefined ||
      title === null ||
      title === '' ||
      title === agent.role ||
      title === roleName(agent.role)
      ? null
      : title;
  };

  const alsoUnder = (agent: AgentNode): string[] => {
    const first = parentOf(agent);
    return agent.startedBy
      .filter(id => id !== first && id !== agent.id && roles.has(id))
      .map(id => roles.get(id) ?? id);
  };

  /** `below[i]`: whether the branch at level i above this one continues under this level's rows. */
  const level = (siblings: readonly Placed[], parent: string, below: readonly boolean[]): void => {
    const done = siblings.filter(node => node.finished);
    const kept = expanded.has(parent) || siblings.some(node => node.holdsSelected);
    const folded = done.length >= FOLD_FROM && !kept;
    const shown = folded ? siblings.filter(node => !node.finished) : siblings;
    const lines = shown.length + (folded ? 1 : 0);
    const guides = (index: number): TreeGuide[] => [
      ...below.map(more => (more ? ('pipe' as const) : ('blank' as const))),
      ...(parent === ROOT_FOLD
        ? []
        : [index === lines - 1 ? ('elbow' as const) : ('tee' as const)]),
    ];
    shown.forEach((node, index) => {
      rows.push({
        kind: 'agent',
        agent: node.agent,
        guides: guides(index),
        alsoUnder: alsoUnder(node.agent),
        headline: headlineOf(node.agent),
      });
      level(
        node.children,
        node.agent.id,
        parent === ROOT_FOLD ? [] : [...below, index < lines - 1],
      );
    });
    if (folded) {
      rows.push({
        kind: 'fold',
        parent,
        count: done.reduce((sum, node) => sum + node.size, 0),
        guides: guides(lines - 1),
      });
    }
  };
  level(roots, ROOT_FOLD, []);
  return rows;
}
