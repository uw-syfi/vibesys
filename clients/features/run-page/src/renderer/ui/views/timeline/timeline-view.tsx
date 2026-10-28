/**
 * The Timeline face: a Gantt over the run clock with zoom. At the run level (`idea` null) one row
 * per idea in the Ideas list's order (rows open ideas, segments open steps; an agent turn in its
 * role's color, a gate hatched, both named by the legend; the end of a bar coloured by outcome,
 * the ratio or verdict at the right; the idea opened beside it selected);
 * the range is the last hour, the last day or all, and a dense run folds its failed ideas into
 * one lane. Within the range the reader zooms and pans (the Gantt's buttons and keys); choosing a
 * range shows all of it again. For an opened idea, the strip of its latest attempt's steps.
 */
import type {Candidate} from '@vibesys/run-state/candidates';
import {
  Button,
  EmptyState,
  Gantt,
  markdownToPlain,
  Row,
  SegmentedControl,
  Stack,
  SwatchLegend,
  Text,
} from '@vibesys/ui';
import {type JSX, useState} from 'react';
import {
  type ClockSpan,
  legendItems,
  TIMELINE_RANGES,
  type TimelineRange,
  type TimelineRow,
  timelineRows,
  timelineZoom,
} from '../../../../state/timeline-rows.js';
import type {ViewBase} from '../props.js';
import {StepStrip} from './step-strip.js';

export interface TimelineViewProps extends ViewBase {
  /** Null: the whole run, one row per idea; else that idea's steps. */
  readonly idea: Candidate | null;
  /** The idea opened beside the whole run's timeline, whose row is selected. */
  readonly selected?: string | null;
}

function RunTimeline(props: ViewBase & {readonly selected: string | null}): JSX.Element {
  const {model, clock, zoom} = props;
  const [range, setRange] = useState<TimelineRange>('all');
  const [expanded, setExpanded] = useState(false);
  // The part of the range looked at, after a zoom or pan; null shows the whole range.
  const [looked, setLooked] = useState<ClockSpan | null>(null);
  const shown = timelineRows(model, range, clock, expanded, looked);
  if (shown.total === 0) {
    return (
      <EmptyState
        title="No ideas yet"
        body={props.emptyLabelsOnly ? '' : 'Each idea the run tries shows on the timeline.'}
      />
    );
  }
  const open = (row: TimelineRow): void => {
    if (row.lane) setExpanded(true);
    else zoom({type: 'open-idea', idea: row.id});
  };
  const key = legendItems(shown.legend);
  const summary =
    shown.folded > 0
      ? `${shown.rows.length - 1} passed and running · failed as one lane`
      : `${shown.rows.length} ideas`;
  return (
    <Stack gap="space-2">
      <Row gap="space-3" align="center">
        <SegmentedControl
          label="Range"
          size="sm"
          options={TIMELINE_RANGES}
          value={range}
          change={next => {
            setRange(next);
            setLooked(null);
          }}
        />
        <Text variant="small" tone="muted">
          {summary}
        </Text>
        {expanded && shown.failedInRange > 0 ? (
          <Button
            size="sm"
            variant="quiet"
            text={`Fold ${shown.failedInRange} failed`}
            press={() => setExpanded(false)}
          />
        ) : null}
      </Row>
      {key.length === 0 ? null : <SwatchLegend label="Bar colors" items={key} />}
      <Gantt
        label="Ideas on the timeline"
        size="fill"
        from={shown.range.from}
        to={shown.range.to}
        view={shown.view}
        changeView={next => setLooked(timelineZoom(next, shown.range))}
        viewLabel={shown.inView}
        {...(clock.now === null ? {} : {playhead: clock.now})}
        ticks={shown.ticks}
        nameLabel="idea"
        figureLabel="vs input"
        rows={shown.rows.map(row => ({
          id: row.id,
          label: row.lane ? row.label : markdownToPlain(row.label),
          start: row.start,
          end: row.end,
          outcome: row.outcome,
          figure: row.figure,
          ...(row.figureTitle === undefined ? {} : {figureTitle: row.figureTitle}),
          selected: row.id === props.selected,
          segments: row.segments.map(segment => {
            const {step} = segment;
            return {
              ...segment,
              ...(row.lane || step === null
                ? {}
                : {press: () => zoom({type: 'open-step', step, idea: row.id})}),
            };
          }),
          press: () => open(row),
        }))}
      />
    </Stack>
  );
}

export function TimelineView(props: TimelineViewProps): JSX.Element {
  const {idea} = props;
  if (idea === null) return <RunTimeline {...props} selected={props.selected ?? null} />;
  const attempt = idea.attempts.at(-1);
  if (attempt === undefined) return <EmptyState title="No steps yet" />;
  return (
    <StepStrip
      idea={idea}
      attempt={attempt}
      now={props.clock.now}
      pauses={props.model.pauses}
      zoom={props.zoom}
    />
  );
}
