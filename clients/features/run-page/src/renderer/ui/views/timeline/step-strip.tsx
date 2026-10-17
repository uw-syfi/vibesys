/**
 * One attempt's steps as a thin Gantt strip over the attempt's own time: each segment opens its
 * step, with a legend naming the roles (and gates) its colors stand for. It is the idea body's
 * strip and the Timeline's face for an opened idea.
 */
import type {Candidate, CandidateAttempt} from '@vibesys/run-state/candidates';
import {Gantt, Stack, SwatchLegend} from '@vibesys/ui';
import type {JSX} from 'react';
import {attemptStrip, legendItems} from '../../../../state/timeline-rows.js';
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
  const key = legendItems(strip.legend);
  return (
    <Stack gap="space-1">
      {key.length === 0 ? null : <SwatchLegend label="Step colors" items={key} />}
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
    </Stack>
  );
}
