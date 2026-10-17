/**
 * The Timeline face: a Gantt over the run clock with zoom. At the run level (`idea` null) one row
 * per idea (rows open ideas, segments open steps; agent turn solid, gate hatched, judge grey); for
 * an opened idea, the strip of its steps.
 *
 * Stub: the frame places it; the Gantt is still to build (#2997).
 */
import type {Candidate} from '@vibesys/run-state/candidates';
import {EmptyState} from '@vibesys/ui';
import type {JSX} from 'react';
import type {ViewBase} from '../props.js';

export interface TimelineViewProps extends ViewBase {
  /** Null: the whole run, one row per idea; else that idea's steps. */
  readonly idea: Candidate | null;
}

export function TimelineView(props: TimelineViewProps): JSX.Element {
  const count = props.model.candidates.candidates.length;
  return <EmptyState title="Timeline" body={`${count} ideas over the run`} />;
}
