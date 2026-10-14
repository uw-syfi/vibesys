/* Generated from vibesys.api.architecture. Do not edit. */

export type SchemaVersion = 1;
export type GraphId = string;
export type Title = string;
export type Description = string;
/**
 * How the producer assembled topology and attribution facts.
 */
export type ArchitectureGenerationApproach = "static-analysis" | "profiler-derived" | "agent-authored" | "hybrid";
export type Source = string;
/**
 * @minItems 1
 * @maxItems 500
 */
export type Components = [ArchitectureComponent, ...ArchitectureComponent[]];
export type Id = string;
export type Label = string;
/**
 * Closed presentation-neutral component categories.
 */
export type ArchitectureComponentKind =
  "api" | "database" | "external" | "library" | "process" | "queue" | "service" | "storage";
/**
 * Stable namespace that gives a component its cross-measurement identity.
 */
export type ArchitectureAnchorKind = "module" | "path" | "service";
export type Value = string;
export type Description1 = string;
export type Group = string | null;
export type Id1 = string;
export type SourceId = string;
export type TargetId = string;
/**
 * Semantic relationship rendered independently of its geometry.
 */
export type ArchitectureEdgeKind = "call" | "data" | "dependency" | "ownership" | "queue" | "storage";
export type Label1 = string | null;
/**
 * @maxItems 2000
 */
export type Edges = ArchitectureEdge[];
export type MeasurementId = string;
export type Sequence = number;
export type Label2 = string;
export type ComponentId = string;
/**
 * @minItems 1
 * @maxItems 2000
 */
export type ChangedFiles = [string, ...string[]];
/**
 * @maxItems 500
 */
export type Changes = ArchitectureComponentChange[];
export type ComponentId1 = string;
export type Value1 = number;
export type Unit = string;
export type Provenance = string;
/**
 * @maxItems 500
 */
export type Attributions = ArchitectureComponentAttribution[];
/**
 * @maxItems 2000
 */
export type Observations = ArchitectureObservation[];

/**
 * The complete version-1 architecture graph and its campaign observations.
 */
export interface ArchitectureGraphDocument {
  schema_version: SchemaVersion;
  graph_id: GraphId;
  title: Title;
  description: Description;
  generation: ArchitectureGeneration;
  components: Components;
  edges: Edges;
  observations: Observations;
}
/**
 * Provenance for one graph document.
 */
export interface ArchitectureGeneration {
  approach: ArchitectureGenerationApproach;
  source: Source;
}
/**
 * One stable node in the system topology.
 */
export interface ArchitectureComponent {
  id: Id;
  label: Label;
  kind: ArchitectureComponentKind;
  anchor: ArchitectureAnchor;
  description: Description1;
  group: Group;
}
/**
 * A stable path, module, or service identity; never a layout coordinate.
 */
export interface ArchitectureAnchor {
  kind: ArchitectureAnchorKind;
  value: Value;
}
/**
 * One stable directed relationship between components.
 */
export interface ArchitectureEdge {
  id: Id1;
  source_id: SourceId;
  target_id: TargetId;
  kind: ArchitectureEdgeKind;
  label: Label1;
}
/**
 * Architecture facts aligned to one campaign measurement cursor.
 */
export interface ArchitectureObservation {
  measurement_id: MeasurementId;
  sequence: Sequence;
  label: Label2;
  changes: Changes;
  attributions: Attributions;
}
/**
 * Recorded files that changed one stable component at an observation.
 */
export interface ArchitectureComponentChange {
  component_id: ComponentId;
  changed_files: ChangedFiles;
}
/**
 * Optional cost and latency slots for one component at one observation.
 */
export interface ArchitectureComponentAttribution {
  component_id: ComponentId1;
  cost: ArchitectureAttributionValue | null;
  latency: ArchitectureAttributionValue | null;
}
/**
 * A non-negative profiler value with explicit units and provenance.
 */
export interface ArchitectureAttributionValue {
  value: Value1;
  unit: Unit;
  provenance: Provenance;
}
