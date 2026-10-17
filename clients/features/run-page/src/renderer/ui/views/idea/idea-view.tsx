/**
 * An opened idea's body (the frame draws its pane, with the idea's facts and "built on"): its
 * attempts (one line each, the latest selected), the step strip over the idea's time, and its
 * steps (agent turns and gates, each headline worded as `span-words` words it), each opening the
 * step; and the compact step list beside an opened step.
 *
 * Stub: the latest attempt's steps as a list; the attempts and the strip are still to build
 * (#2997).
 */
import type {Candidate, CandidateStep} from '@vibesys/run-state/candidates';
import {EmptyState, PickList, PickRow} from '@vibesys/ui';
import type {JSX} from 'react';
import type {ViewBase, Zoom} from '../props.js';

export interface IdeaViewProps extends ViewBase {
  readonly idea: Candidate;
}

export function IdeaView(props: IdeaViewProps): JSX.Element {
  return <StepList idea={props.idea} selected={null} zoom={props.zoom} />;
}

export interface StepListProps {
  readonly idea: Candidate;
  /** The opened step's span, highlighted; null when none is open. */
  readonly selected: string | null;
  readonly zoom: Zoom;
}

function stateOf(step: CandidateStep): {tone: 'live' | 'ok' | 'err' | 'neutral'; label: string} {
  switch (step.status) {
    case 'running':
    case 'waiting':
    case 'pending':
      return {tone: 'live', label: step.status};
    case 'done':
      return {tone: 'ok', label: 'done'};
    case 'failed':
      return {tone: 'err', label: 'failed'};
    default:
      return {tone: 'neutral', label: step.status};
  }
}

/** The steps of the idea's latest attempt, each opening its step. */
export function StepList(props: StepListProps): JSX.Element {
  const steps = props.idea.attempts.at(-1)?.steps ?? [];
  if (steps.length === 0) return <EmptyState title="No steps yet" />;
  return (
    <PickList
      label={`${props.idea.title} steps`}
      size="fill"
      {...(props.selected === null ? {} : {selected: props.selected})}
    >
      {steps.map(step => (
        <PickRow
          key={step.span}
          id={step.span}
          {...(step.role === null ? {} : {lead: step.role})}
          title={step.title}
          state={stateOf(step)}
          select={() => props.zoom({type: 'open-step', step: step.span})}
        />
      ))}
    </PickList>
  );
}
