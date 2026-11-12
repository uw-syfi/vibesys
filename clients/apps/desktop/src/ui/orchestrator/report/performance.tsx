import {
  Button,
  Card,
  Chart,
  ChartDetail,
  ChartLegend,
  type ChartMarker,
  type ChartNote,
  type ChartPoint,
  type ChartReference,
  type ChartSize,
  type ChartTick,
  Chip,
  chartPlotMargins,
  RangeStrip,
  Row,
  SegmentedControl,
  type SegmentOption,
  Stack,
  Text,
} from '@vibesys/ui';
import {type JSX, useState} from 'react';
import {
  type BandView,
  type Better,
  bandView,
  choiceOf,
  historyStart,
  type IdeasReport,
  type ObjectiveReport,
  offersRanges,
  type RangeChoice,
  type RangeView,
  rangeChoices,
  type Span,
  valueTicks,
  WHOLE_RUN,
  windowOf,
} from '../../../state/index.js';
import {shortCommit} from '../../commit-text.js';
import {useDisplayClock} from '../../display-clock.js';
import type {DisplayClock} from '../../display-time.js';
import {nameOf} from '../names.js';
import {clip, formatAge, formatExact, formatFigure, withUnit} from './format.js';
import {ObjectiveSummary} from './objective-summary.js';

/** The longest note of a bound a hover writes whole; the agent's own words go on after it in its store. */
const NOTE_LIMIT = 600;
/** How many labels a value axis aims for. */
const Y_LABELS = 3;
/** The most measurements a chart writes the value of, beside the corners of its line. */
const NOTED_UP_TO = 20;

/** How a chart point links to the ideas: what the host served, the point chosen and the idea singled out. */
export interface PointLinks {
  readonly ideas: IdeasReport | undefined;
  /** The measure job of the point chosen, or null. */
  readonly picked: string | null;
  /** The jobs of the idea singled out in the table: lit on every chart. */
  readonly lit: ReadonlySet<string>;
  readonly pick: (jobId: string) => void;
  /** Open the idea's row in the table. */
  readonly showIdea: (plan: number) => void;
}

/** The plans a point's job names: none for a `--no-plan` one; undefined while no ideas are served. */
const plansOf = (links: PointLinks, jobId: string): readonly number[] | undefined =>
  links.ideas?.plansOfJob.get(jobId);

/** `#7`, `#7 #9` or `no plan`: what a point says of the idea it measured. */
function planWords(plans: readonly number[] | undefined): string {
  if (plans === undefined) return '';
  return plans.length === 0 ? 'no plan' : plans.map(plan => `#${plan}`).join(' ');
}

/** Better is always up: a value of an objective that is better lower is drawn negated, and written as itself. */
const upward = (better: Better, value: number): number => (better === 'lower' ? -value : value);

function boundReference(
  objective: ObjectiveReport,
  names: ReadonlyMap<string, string>,
  clock: DisplayClock,
): ChartReference | undefined {
  const bound = objective.bound?.current;
  if (bound === undefined || objective.better === null) return undefined;
  const who = [
    bound.agent === '' ? '' : nameOf(names, bound.agent),
    bound.atMs === null ? '' : clock.reportMoment(bound.atMs),
  ]
    .filter(part => part !== '')
    .join(' · ');
  return {
    y: upward(objective.better, bound.value),
    label: `Bound ${formatFigure(bound.value)}`,
    hint: [withUnit(formatExact(bound.value), bound.unit), clip(bound.note, NOTE_LIMIT), who]
      .filter(line => line !== '')
      .join('\n'),
  };
}

/** What a band's value axis spans, drawn upward, and the labels at its ticks, written as the values they stand for. */
function valueAxis(
  better: Better,
  band: BandView,
): {readonly domain: readonly [number, number]; readonly ticks: readonly ChartTick[]} {
  const [low, high] = band.domain;
  const domain: readonly [number, number] =
    better === 'lower' ? [upward(better, high), upward(better, low)] : [low, high];
  return {
    domain,
    ticks: valueTicks(domain[0], domain[1], Y_LABELS).map(value => ({
      value,
      label: formatFigure(upward(better, value)),
    })),
  };
}

/** The words an empty chart stands on: why there is nothing to draw, in one line. */
function emptyWords(props: {
  readonly measured: number;
  readonly runOver: boolean;
  readonly unreadable: boolean;
  readonly startedAgo: string;
}): string {
  if (props.measured > 0) return 'Nothing measured in this range';
  if (props.runOver) return 'Nothing was measured';
  if (props.unreadable) return 'The measurements cannot be read now';
  return `Waiting for the input's measurement · started ${props.startedAgo}`;
}

/** What the marker on the time axis says on hover: why the line is all there is before it. */
const HISTORY_HINT =
  'The host keeps the measurements that set a best, and only the newest of the others. Before this line, the best-so-far line stands for the rest; from here, every measurement is drawn.';

/** One objective's chart: its dots and best-so-far line over the shared time axis, at a height that never changes. */
function ObjectiveChart(props: {
  readonly objective: ObjectiveReport;
  readonly band: BandView;
  readonly window: Span;
  readonly size: ChartSize;
  readonly names: ReadonlyMap<string, string>;
  /** The widest value label of any chart on this axis, so every plot starts at one left edge. */
  readonly labelChars: number;
  readonly links: PointLinks;
  readonly empty: string;
  /** From when every measurement is drawn, when the host kept only the newest of the others; null otherwise. */
  readonly historyFrom: number | null;
}): JSX.Element {
  const {objective, band, window} = props;
  const clock = useDisplayClock();
  const {better} = objective;
  const axis = better === null ? {domain: band.domain, ticks: []} : valueAxis(better, band);
  const points: ChartPoint[] =
    better === null
      ? []
      : band.points.map(point => ({
          key: point.id,
          id: point.id,
          x: point.atMs,
          y: upward(better, point.value),
          selected: props.links.picked === point.id,
          lit: props.links.lit.has(point.id),
          label: [
            shortCommit(point.commit),
            planWords(plansOf(props.links, point.id)),
            withUnit(formatExact(point.value), objective.unit),
          ]
            .filter(part => part !== '')
            .join(' · '),
          tip: {
            lead: [shortCommit(point.commit), planWords(plansOf(props.links, point.id))]
              .filter(part => part !== '')
              .join(' · '),
            detail: withUnit(formatExact(point.value), objective.unit),
          },
        }));
  const notes: ChartNote[] =
    better === null || band.points.length > NOTED_UP_TO
      ? []
      : band.corners.map(point => ({
          x: point.atMs,
          y: upward(better, point.value),
          label: formatFigure(point.value),
        }));
  const from = props.historyFrom;
  const marker: ChartMarker | undefined =
    from === null || from <= window.fromMs || from >= window.toMs
      ? undefined
      : {x: from, label: 'all measurements from here', hint: HISTORY_HINT};
  const reference = band.boundDrawn ? boundReference(objective, props.names, clock) : undefined;
  return (
    <Chart
      label={`${objective.name} of every measured candidate over time, with the best so far`}
      size={props.size}
      points={points}
      steps={
        better === null ? [] : band.steps.map(step => ({x: step.x, y: upward(better, step.y)}))
      }
      notes={notes}
      xDomain={[window.fromMs, Math.max(window.toMs, window.fromMs + 1)]}
      timeAxis={{label: clock.reportTick, zone: clock.offsetMs}}
      yDomain={axis.domain}
      yTicks={band.points.length === 0 ? [] : axis.ticks}
      yLabelChars={props.labelChars}
      pick={props.links.pick}
      {...(band.points.length === 0 ? {empty: props.empty} : {})}
      {...(marker === undefined ? {} : {marker})}
      {...(reference === undefined ? {} : {references: [reference]})}
    />
  );
}

/** The key to what the charts draw: every mark and color, the ones not on screen now kept in place. */
function Key(props: {
  readonly bound: boolean;
  readonly picked: boolean;
  readonly lit: boolean;
}): JSX.Element {
  return (
    <ChartLegend
      label="Key to the charts"
      items={[
        {label: 'Best so far', mark: 'line'},
        {label: 'Measured', mark: 'dot'},
        {label: 'Bound', mark: 'dash', reserved: !props.bound},
        {label: 'Chosen point', mark: 'ring', reserved: !props.picked},
        {label: 'Idea’s measurements', mark: 'chain', reserved: !props.lit},
      ]}
    />
  );
}

type Picked = RangeChoice | 'dragged';

const RANGE_WORDS: Readonly<Record<RangeChoice, string>> = {
  hour: '1h',
  'six-hours': '6h',
  day: '1d',
  all: 'All',
};

const RANGE_SPOKEN: Readonly<Record<RangeChoice, string>> = {
  hour: 'Last hour',
  'six-hours': 'Last 6 hours',
  day: 'Last day',
  all: 'Whole run',
};

/** The range control: the last hour, six hours, a day or all of a run that is long. */
function RangeControl(props: {
  readonly span: Span;
  readonly view: RangeView;
  readonly change: (view: RangeView) => void;
}): JSX.Element {
  const options: SegmentOption<Picked>[] = rangeChoices(props.span).map(choice => ({
    value: choice,
    label: RANGE_WORDS[choice],
  }));
  return (
    <SegmentedControl<Picked>
      label="Range"
      size="xs"
      options={options}
      value={choiceOf(props.view) ?? 'dragged'}
      change={picked => {
        if (picked === 'dragged') return;
        props.change(picked === 'all' ? WHOLE_RUN : {kind: 'last', choice: picked});
      }}
    />
  );
}

/** The whole run as a thin strip with the window on it, to drag out a window. */
function Overview(props: {
  readonly objective: ObjectiveReport | undefined;
  /** Where the charts' plots start and stop, so the strip lines up with them. */
  readonly inset: {readonly left: number; readonly right: number};
  readonly span: Span;
  readonly window: Span;
  readonly view: RangeView;
  readonly change: (view: RangeView) => void;
}): JSX.Element {
  const {span, window, objective} = props;
  const clock = useDisplayClock();
  const whole = props.view.kind === 'all';
  const words =
    props.view.kind === 'between'
      ? `${clock.reportMoment(window.fromMs)} to ${clock.reportMoment(window.toMs)}`
      : RANGE_SPOKEN[choiceOf(props.view) ?? 'all'];
  const entire = objective === undefined ? undefined : bandView(objective, span);
  const better = objective?.better;
  return (
    <Row gap="space-4" rowGap="space-2" wrap align="center">
      <Stack gap="space-0" width="summary">
        <Text variant="small" tone="muted">
          Drag to zoom
        </Text>
      </Stack>
      <Stack gap="space-0" width="chart">
        <RangeStrip
          label="The whole run, to choose a window of it"
          valueText={words}
          domain={[span.fromMs, span.toMs]}
          inset={props.inset}
          window={whole ? null : [window.fromMs, window.toMs]}
          steps={
            better === undefined || better === null
              ? []
              : (entire?.steps ?? []).map(step => ({
                  x: step.x,
                  y: upward(better, step.y),
                }))
          }
          change={chosen =>
            props.change(
              chosen === null ? WHOLE_RUN : {kind: 'between', fromMs: chosen[0], toMs: chosen[1]},
            )
          }
        />
      </Stack>
    </Row>
  );
}

/**
 * What the point chosen on a chart is: its job's commit, its value, and the idea each plan it names
 * stands for, with a button to that idea's row. A job that names no plan says so. The line holds its
 * height with no point chosen, and says what choosing one does, so choosing one moves nothing.
 */
function PointDetail(props: {
  readonly objectives: readonly ObjectiveReport[];
  readonly links: PointLinks;
}): JSX.Element {
  const {links} = props;
  const found = props.objectives.flatMap(objective =>
    objective.points.filter(point => point.id === links.picked).map(point => ({objective, point})),
  )[0];
  if (found === undefined) {
    const choosable =
      links.ideas !== undefined && props.objectives.some(each => each.points.length > 0);
    return (
      <ChartDetail label="Chosen point">
        {choosable ? (
          <Text variant="small" tone="muted">
            Choose a point to see the idea it measured
          </Text>
        ) : null}
      </ChartDetail>
    );
  }
  const {objective, point} = found;
  const plans = plansOf(links, point.id);
  return (
    <ChartDetail label="Chosen point">
      <Text variant="code">{shortCommit(point.commit)}</Text>
      <Text variant="small" tone="muted">
        {`${objective.name} ${withUnit(formatExact(point.value), objective.unit)}`}
      </Text>
      {plans === undefined ? null : plans.length === 0 ? (
        <Chip label="No plan" />
      ) : (
        plans.map(plan => {
          const idea = links.ideas?.ideas.find(each => each.planNumber === plan);
          return idea === undefined ? (
            <Chip key={plan} label={`Idea #${plan}`} />
          ) : (
            <Button
              key={plan}
              variant="quiet"
              size="sm"
              text={`#${plan} ${clip(idea.title, TITLE_LIMIT)}`}
              label={`Show idea ${plan}`}
              hint={idea.title}
              press={() => links.showIdea(plan)}
            />
          );
        })
      )}
    </ChartDetail>
  );
}

/** The longest idea title the line under the charts writes; the table has it whole. */
const TITLE_LIMIT = 48;

/**
 * Every objective over time: a band for each, its summary beside its own chart, all on one time axis.
 * The frame is the same from the first minute to the last: a chart is the same height whatever the
 * number of points (200 px for one objective, 150 for several), an objective with no measurement
 * keeps its band with the axis and a line of what it waits for, and a run with no objective yet
 * holds one band for the measurements to come. A run longer than six hours offers a range and an
 * overview strip.
 */
export function PerformancePanel(props: {
  readonly objectives: readonly ObjectiveReport[];
  readonly span: Span;
  readonly nowMs: number;
  readonly runOver: boolean;
  /** The host cannot read the run's job log now. */
  readonly unreadable: boolean;
  /** The host served only part of the measurements. */
  readonly truncated: boolean;
  /** What each agent is called on screen, by agent id. */
  readonly names: ReadonlyMap<string, string>;
  readonly links: PointLinks;
}): JSX.Element {
  const {objectives, span} = props;
  const clock = useDisplayClock();
  const [view, setView] = useState<RangeView>(WHOLE_RUN);
  const window = windowOf(view, span);
  const long = offersRanges(span);
  const bands = objectives.map(objective => ({objective, band: bandView(objective, window)}));
  const labelChars = Math.max(
    0,
    ...bands.flatMap(({objective, band}) =>
      objective.better === null
        ? []
        : valueAxis(objective.better, band).ticks.map(tick => tick.label.length),
    ),
  );
  const size: ChartSize = objectives.length > 1 ? 'strip' : 'tall';
  const empty = (measured: number): string =>
    emptyWords({
      measured,
      runOver: props.runOver,
      unreadable: props.unreadable,
      startedAgo: formatAge(props.nowMs - span.fromMs),
    });
  const picked = props.links.picked !== null;
  return (
    <Card
      dense
      section="Performance"
      beside={
        <Key
          bound={bands.some(({band}) => band.boundDrawn)}
          picked={picked}
          lit={props.links.lit.size > 0}
        />
      }
      action={long ? <RangeControl span={span} view={view} change={setView} /> : undefined}
    >
      <Stack gap="space-5">
        {bands.length === 0 ? (
          <Row gap="space-4" rowGap="space-3" wrap align="stretch">
            <Stack gap="space-1" width="summary">
              <Text variant="item">Measurements</Text>
              <Text variant="small" tone="muted">
                {props.runOver ? 'Nothing was measured' : 'Nothing measured yet'}
              </Text>
            </Stack>
            <Stack gap="space-0" width="chart">
              <Chart
                label="Nothing has been measured yet"
                size={size}
                points={[]}
                xDomain={[window.fromMs, Math.max(window.toMs, window.fromMs + 1)]}
                timeAxis={{label: clock.reportTick, zone: clock.offsetMs}}
                empty={empty(0)}
              />
            </Stack>
          </Row>
        ) : (
          bands.map(({objective, band}) => (
            <Row key={objective.name} gap="space-4" rowGap="space-3" wrap align="stretch">
              <Stack gap="space-1" width="summary">
                <ObjectiveSummary
                  objective={objective}
                  runOver={props.runOver}
                  inView={
                    view.kind === 'all' || !long
                      ? null
                      : {shown: band.points.length, of: objective.points.length}
                  }
                />
              </Stack>
              <Stack gap="space-0" width="chart">
                <ObjectiveChart
                  objective={objective}
                  band={band}
                  window={window}
                  size={size}
                  names={props.names}
                  labelChars={labelChars}
                  links={props.links}
                  empty={empty(objective.points.length)}
                  historyFrom={historyStart(objective, props.truncated)}
                />
              </Stack>
            </Row>
          ))
        )}
        {long ? (
          <Overview
            objective={objectives.find(each => each.points.length > 0)}
            inset={chartPlotMargins(labelChars)}
            span={span}
            window={window}
            view={view}
            change={setView}
          />
        ) : null}
        <PointDetail objectives={objectives} links={props.links} />
      </Stack>
    </Card>
  );
}
