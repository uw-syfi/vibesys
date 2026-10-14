/** Deterministic dependency-layer layout; identity never depends on coordinates or input order. */
import type {ArchitectureGraphDocument} from '@vibesys/contracts';

export interface ArchitectureNodeLayout {
  readonly id: string;
  readonly x: number;
  readonly y: number;
  readonly width: number;
  readonly height: number;
}

export interface ArchitectureEdgeLayout {
  readonly id: string;
  readonly path: string;
}

export interface ArchitectureLayout {
  readonly width: number;
  readonly height: number;
  readonly nodes: readonly ArchitectureNodeLayout[];
  readonly edges: readonly ArchitectureEdgeLayout[];
}

const NODE_WIDTH = 220;
const NODE_HEIGHT = 124;
const COLUMN_GAP = 100;
const ROW_GAP = 36;
const MARGIN = 32;

export function layoutArchitecture(document: ArchitectureGraphDocument): ArchitectureLayout {
  const ranks = componentRanks(document);
  const layers = layersFromRanks(ranks);
  const nodes = placeNodes(layers);
  const byId = new Map(nodes.map(node => [node.id, node]));
  const edges = [...document.edges]
    .sort((left, right) => left.id.localeCompare(right.id))
    .map(edge => ({
      id: edge.id,
      path: edgePath(requiredNode(byId, edge.source_id), requiredNode(byId, edge.target_id)),
    }));
  return {
    width: MARGIN * 2 + layers.length * NODE_WIDTH + Math.max(0, layers.length - 1) * COLUMN_GAP,
    height:
      MARGIN * 2 +
      Math.max(...layers.map(layer => layer.length), 1) * NODE_HEIGHT +
      Math.max(...layers.map(layer => Math.max(0, layer.length - 1)), 0) * ROW_GAP,
    nodes,
    edges,
  };
}

function componentRanks(document: ArchitectureGraphDocument): ReadonlyMap<string, number> {
  const ids = document.components.map(component => component.id).sort();
  const {indegree, outgoing} = dependencyIndex(document, ids);
  const ranks = new Map(ids.map(id => [id, 0]));
  const visited = rankAcyclicPrefix(ids, indegree, outgoing, ranks);
  const fallbackRank = Math.max(...ranks.values(), 0) + 1;
  for (const id of ids) if (!visited.has(id)) ranks.set(id, fallbackRank);
  return ranks;
}

function dependencyIndex(
  document: ArchitectureGraphDocument,
  ids: readonly string[],
): {
  readonly indegree: Map<string, number>;
  readonly outgoing: Map<string, string[]>;
} {
  const indegree = new Map(ids.map(id => [id, 0]));
  const outgoing = new Map(ids.map(id => [id, [] as string[]]));
  for (const edge of document.edges) {
    indegree.set(edge.target_id, (indegree.get(edge.target_id) ?? 0) + 1);
    outgoing.get(edge.source_id)?.push(edge.target_id);
  }
  for (const targets of outgoing.values()) targets.sort();
  return {indegree, outgoing};
}

function rankAcyclicPrefix(
  ids: readonly string[],
  indegree: Map<string, number>,
  outgoing: ReadonlyMap<string, readonly string[]>,
  ranks: Map<string, number>,
): ReadonlySet<string> {
  const ready = ids.filter(id => indegree.get(id) === 0);
  const visited = new Set<string>();
  while (ready.length > 0) {
    const id = ready.shift();
    if (id === undefined) break;
    visited.add(id);
    for (const target of outgoing.get(id) ?? []) {
      ranks.set(target, Math.max(ranks.get(target) ?? 0, (ranks.get(id) ?? 0) + 1));
      indegree.set(target, (indegree.get(target) ?? 1) - 1);
      if (indegree.get(target) === 0) insertSorted(ready, target);
    }
  }
  return visited;
}

function layersFromRanks(ranks: ReadonlyMap<string, number>): string[][] {
  const highest = Math.max(...ranks.values(), 0);
  const layers = Array.from({length: highest + 1}, () => [] as string[]);
  for (const [id, rank] of [...ranks.entries()].sort(([left], [right]) =>
    left.localeCompare(right),
  )) {
    layers[rank]?.push(id);
  }
  return layers.filter(layer => layer.length > 0);
}

function placeNodes(layers: readonly (readonly string[])[]): ArchitectureNodeLayout[] {
  const largest = Math.max(...layers.map(layer => layer.length), 1);
  return layers.flatMap((layer, column) => {
    const offset = ((largest - layer.length) * (NODE_HEIGHT + ROW_GAP)) / 2;
    return layer.map((id, row) => ({
      id,
      x: MARGIN + column * (NODE_WIDTH + COLUMN_GAP),
      y: MARGIN + offset + row * (NODE_HEIGHT + ROW_GAP),
      width: NODE_WIDTH,
      height: NODE_HEIGHT,
    }));
  });
}

function edgePath(source: ArchitectureNodeLayout, target: ArchitectureNodeLayout): string {
  const startX = source.x + source.width;
  const startY = source.y + source.height / 2;
  const endX = target.x;
  const endY = target.y + target.height / 2;
  const bend = startX + (endX - startX) / 2;
  return `M ${startX} ${startY} C ${bend} ${startY}, ${bend} ${endY}, ${endX} ${endY}`;
}

function requiredNode(
  nodes: ReadonlyMap<string, ArchitectureNodeLayout>,
  id: string,
): ArchitectureNodeLayout {
  const node = nodes.get(id);
  if (node === undefined) throw new Error(`Architecture layout is missing component ${id}`);
  return node;
}

function insertSorted(values: string[], value: string): void {
  const index = values.findIndex(item => item.localeCompare(value) > 0);
  values.splice(index < 0 ? values.length : index, 0, value);
}
