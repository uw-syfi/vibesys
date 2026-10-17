/**
 * One attempt's steps as a thin Gantt strip over the attempt's own time: each segment opens its
 * step. It is the idea body's strip and the Timeline's face for an opened idea.
 */
import type {Candidate, CandidateAttempt} from '@vibesys/run-state/candidates';
import {Gantt} from '@vibesys/ui';
import type {JSX} from 'react';
import {attemptStrip} from '../../../../state/timeline-rows.js';
import type {Zoom} from '../props.js';

export function StepStrip(props: {
  readonly idea: Candidate;
  readonly attempt: CandidateAttempt;
  readonly now: number | null;
  readonly zoom: Zoom;
}): JSX.Element {
  const {idea, attempt, zoom} = props;
  const strip = attemptStrip(idea, attempt, props.now);
  const {row} = strip;
  return (
    <Gantt
      label={row.label}
      strip
      from={strip.from}
      to={strip.to}
      {...(strip.playhead === null ? {} : {playhead: strip.playhead})}
      rows={[
        {
          id: row.id,
          label: row.label,
          start: row.start,
          end: row.end,
          outcome: row.outcome,
          figure: row.figure,
          selected: false,
          segments: row.segments.map(segment => ({
            ...segment,
            press: () => zoom({type: 'open-step', step: segment.id}),
          })),
          press: () => undefined,
        },
      ]}
    />
  );
}
