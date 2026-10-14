/**
 * Strict client boundary for the versioned architecture graph published by
 * `vibesys.api.architecture`. Cross-reference checks mirror the Pydantic model validators; the
 * generated descriptor owns every structural field, constraint, and closed set.
 */
import {BackendClientError} from './errors.js';
import type {
  ArchitectureAnchor,
  ArchitectureAnchorKind,
  ArchitectureAttributionValue,
  ArchitectureComponent,
  ArchitectureComponentAttribution,
  ArchitectureComponentChange,
  ArchitectureComponentKind,
  ArchitectureEdge,
  ArchitectureEdgeKind,
  ArchitectureGeneration,
  ArchitectureGenerationApproach,
  ArchitectureGraphDocument,
  ArchitectureObservation,
} from './generated/architecture-graph.generated.js';
import architectureGraphSchema from './generated/architecture-graph.schema.js';
import {type SchemaWalk, validateSchema} from './schema-walker.js';

export type {
  ArchitectureAnchor,
  ArchitectureAnchorKind,
  ArchitectureAttributionValue,
  ArchitectureComponent,
  ArchitectureComponentAttribution,
  ArchitectureComponentChange,
  ArchitectureComponentKind,
  ArchitectureEdge,
  ArchitectureEdgeKind,
  ArchitectureGeneration,
  ArchitectureGenerationApproach,
  ArchitectureGraphDocument,
  ArchitectureObservation,
};

const STRICT: SchemaWalk = {documents: [architectureGraphSchema], strict: true};

/** Validate untrusted graph JSON structurally and semantically before returning generated types. */
export function parseArchitectureGraph(value: unknown): ArchitectureGraphDocument {
  validateSchema(value, architectureGraphSchema, 'architecture graph', STRICT);
  const graph = value as ArchitectureGraphDocument;
  const componentIds = uniqueIds(graph.components, 'components', component => component.id);
  uniqueIds(graph.edges, 'edges', edge => edge.id);
  uniqueIds(graph.observations, 'observations', observation => observation.measurement_id);

  for (const [index, edge] of graph.edges.entries()) {
    requireComponent(edge.source_id, componentIds, `edges[${index}].source_id`);
    requireComponent(edge.target_id, componentIds, `edges[${index}].target_id`);
    if (edge.source_id === edge.target_id) invalid(`edges[${index}]`, 'must not be a self-edge');
  }

  let previousSequence = -1;
  for (const [index, observation] of graph.observations.entries()) {
    const path = `observations[${index}]`;
    if (observation.sequence <= previousSequence) {
      invalid(`${path}.sequence`, `must be strictly greater than ${previousSequence}`);
    }
    previousSequence = observation.sequence;
    const changeIds = uniqueIds(
      observation.changes,
      `${path}.changes`,
      change => change.component_id,
    );
    for (const [changeIndex, change] of observation.changes.entries()) {
      const changedFilesPath = `${path}.changes[${changeIndex}].changed_files`;
      uniqueValues(change.changed_files, changedFilesPath);
      for (const [fileIndex, file] of change.changed_files.entries()) {
        requireNormalizedRelativePath(file, `${changedFilesPath}[${fileIndex}]`);
      }
    }
    for (const componentId of changeIds) {
      requireComponent(componentId, componentIds, `${path}.changes`);
    }
    const attributionIds = uniqueIds(
      observation.attributions,
      `${path}.attributions`,
      attribution => attribution.component_id,
    );
    for (const componentId of attributionIds) {
      requireComponent(componentId, componentIds, `${path}.attributions`);
    }
  }
  return graph;
}

function uniqueIds<T>(
  values: readonly T[],
  path: string,
  identity: (value: T) => string,
): Set<string> {
  const result = new Set<string>();
  for (const [index, value] of values.entries()) {
    const id = identity(value);
    if (result.has(id)) invalid(`${path}[${index}]`, `duplicates ${JSON.stringify(id)}`);
    result.add(id);
  }
  return result;
}

function uniqueValues(values: readonly string[], path: string): void {
  const unique = new Set(values);
  if (unique.size !== values.length) invalid(path, 'contains a duplicate value');
}

function requireNormalizedRelativePath(value: string, path: string): void {
  const parts = value.split('/');
  if (
    value === '' ||
    value.startsWith('/') ||
    value.includes('\\') ||
    parts.some(part => part === '' || part === '.' || part === '..')
  ) {
    invalid(path, `must be a normalized relative path, received ${JSON.stringify(value)}`);
  }
}

function requireComponent(value: string, known: ReadonlySet<string>, path: string): void {
  if (!known.has(value)) invalid(path, `references unknown component ${JSON.stringify(value)}`);
}

function invalid(path: string, reason: string): never {
  throw new BackendClientError('parse', `Invalid server architecture graph ${path}: ${reason}`);
}
