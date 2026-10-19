/** Run models of ideas built on each other, for the lineage tests: plain mocked run data. */
import type {Candidate, CandidateOutcome, CandidateTree} from '@vibesys/run-state/candidates';
import type {RunModel} from './run-model.js';
import {idea} from './timeline-fixtures.js';

export interface Shape {
  /** Each idea's base: the index of an earlier idea, or null for the input. */
  readonly parents: readonly (number | null)[];
  readonly outcomes?: readonly CandidateOutcome[];
  /** The index of the best idea; none when absent. */
  readonly best?: number;
}

export const idOf = (index: number): string => `n${index}`;

/** The candidates of `shape`: `n0`, `n1`, … titled `idea 0`, … each with a ratio of its index. */
function candidatesOf(shape: Shape): Candidate[] {
  return shape.parents.map((parent, index) => {
    const outcome = shape.outcomes?.[index] ?? 'passed';
    const base = idea(idOf(index), outcome, index * 10, 5);
    const measured = base.measurement;
    return {
      ...base,
      title: `idea ${index}`,
      parent: parent === null ? null : idOf(parent),
      measurement:
        measured === null
          ? null
          : {...measured, metricValue: 100 + index, vsBaseline: 1 + index / 100},
    };
  });
}

/** The run model of `shape`, with the best chain the candidate fold would give. */
export function modelOfShape(shape: Shape): RunModel {
  const candidates = candidatesOf(shape);
  const byId = new Map(candidates.map(item => [item.id, item]));
  const bestPath: string[] = [];
  if (shape.best !== undefined) {
    for (let at = byId.get(idOf(shape.best)); at !== undefined; at = byId.get(at.parent ?? '')) {
      bestPath.unshift(at.id);
    }
  }
  const tree: CandidateTree = {
    candidates,
    best: shape.best === undefined ? null : {candidate: idOf(shape.best), adopted: false},
    bestPath,
    adoptionOpened: shape.best !== undefined,
  };
  return {
    candidates: tree,
    direction: 'max',
    overview: null,
    at: null,
    latest: null,
    inputGates: [],
    changed: new Set<string>(),
    agents: {agents: []},
    trace: {spans: new Map()},
  } as unknown as RunModel;
}
