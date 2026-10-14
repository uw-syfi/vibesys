/** Pure architecture history fold driven by the campaign's authoritative measurement cursor. */
import type {
  ArchitectureComponentAttribution,
  ArchitectureGraphDocument,
  ArchitectureObservation,
} from '@vibesys/contracts';

export interface ArchitectureCursor {
  readonly measurementId: string | null;
  readonly sequence: number;
}

export interface ArchitectureState {
  readonly document: ArchitectureGraphDocument;
  readonly cursor: ArchitectureCursor;
}

export interface ArchitectureView {
  readonly document: ArchitectureGraphDocument;
  readonly observation: ArchitectureObservation | null;
  readonly changedComponentIds: ReadonlySet<string>;
  readonly changedFiles: readonly string[];
  readonly attributions: ReadonlyMap<string, ArchitectureComponentAttribution>;
}

export type ArchitectureAction = {
  readonly type: 'campaign-cursor';
  readonly cursor: ArchitectureCursor;
};

export function initialArchitectureState(document: ArchitectureGraphDocument): ArchitectureState {
  return {document, cursor: {measurementId: null, sequence: -1}};
}

/** Apply one shared campaign cursor. Replaying the same cursor is referentially idempotent. */
export function foldArchitectureCursor(
  state: ArchitectureState,
  action: ArchitectureAction,
): ArchitectureState {
  if (
    state.cursor.measurementId === action.cursor.measurementId &&
    state.cursor.sequence === action.cursor.sequence
  ) {
    return state;
  }
  return {...state, cursor: action.cursor};
}

/** Project only facts recorded at or before the selected campaign measurement. */
export function projectArchitecture(state: ArchitectureState): ArchitectureView {
  const observation = observationAtCursor(state.document.observations, state.cursor);
  const changes =
    observation?.measurement_id === state.cursor.measurementId &&
    observation.sequence === state.cursor.sequence
      ? observation.changes
      : [];
  return {
    document: state.document,
    observation,
    changedComponentIds: new Set(changes.map(change => change.component_id)),
    changedFiles: [...new Set(changes.flatMap(change => change.changed_files))],
    attributions: new Map(
      (observation?.attributions ?? []).map(attribution => [attribution.component_id, attribution]),
    ),
  };
}

function observationAtCursor(
  observations: readonly ArchitectureObservation[],
  cursor: ArchitectureCursor,
): ArchitectureObservation | null {
  let latest: ArchitectureObservation | null = null;
  for (const observation of observations) {
    if (observation.sequence > cursor.sequence) break;
    latest = observation;
    if (observation.measurement_id === cursor.measurementId) return observation;
  }
  return latest;
}
