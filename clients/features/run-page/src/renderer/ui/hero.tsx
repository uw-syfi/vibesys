/**
 * The performance row: a chart per objective of the run, each under its name and its headline (the
 * best so far as a multiple of the input), side by side; with exactly two objectives the trade-off
 * chart (its points and the frontier of the best trade-offs) stands beside them; under them the key
 * to what the charts draw. Each chart is the best so far as one line over a dot for each reading, on
 * the run's running time; the time bar's position is a line on every chart, the dots and the line
 * after it dimmed. With an idea open, its reading is ringed and the steps its lineage set are ringed
 * in the accent; hovering a reading says its idea, value and multiple.
 * The Run screen draws it above the level, so it is the same element at every level and navigating
 * moves nothing in it: the panes below change, this row does not.
 */
import {
  Chart,
  ChartColumns,
  ChartLegend,
  ChartPanel,
  Fact,
  FactLine,
  ParetoChart,
  Stack,
  Text,
} from '@vibesys/ui';
import type {JSX} from 'react';
import {
  type ChartLegendEntry,
  chartLegend,
  type HeroView,
  heroView,
  type ObjectiveChartView,
  type ObjectiveHeadline,
  objectiveCharts,
  objectivesOf,
  paretoView,
  type RunModel,
} from '../../state/index.js';
import type {ViewBase} from './views/props.js';

/** The comparisons with no reading, each a small label with its status word; none when all read. */
function BaselineNotes(props: {readonly notes: HeroView['baselineNotes']}): JSX.Element | null {
  if (props.notes.length === 0) return null;
  return (
    <FactLine fit="parent" wrap={false}>
      {props.notes.map(note => (
        <Fact key={note.label} title={`${note.label} ${note.words}`}>
          <Text variant="small" tone="muted">
            {`${note.label} · ${note.words}`}
          </Text>
        </Fact>
      ))}
    </FactLine>
  );
}

const LEGEND_WORDS: Readonly<
  Record<ChartLegendEntry, {readonly label: string; readonly mark: 'line' | 'dot' | 'ring'}>
> = {
  best: {label: 'Best so far', mark: 'line'},
  reading: {label: 'Reading', mark: 'dot'},
  selected: {label: 'Selected', mark: 'ring'},
};

/** What a chart's heading says of which way is better. */
const BETTER_WORDS = {higher: 'higher is better', lower: 'lower is better'} as const;

/** One objective: its heading with the headline, and its chart. */
function ObjectivePanel(props: {
  readonly chart: ObjectiveChartView;
  readonly headline: ObjectiveHeadline | undefined;
  readonly zoom: ViewBase['zoom'];
  readonly hover: ((idea: string | null) => void) | undefined;
}): JSX.Element {
  const {chart, headline} = props;
  return (
    <ChartPanel
      title={headline?.name ?? chart.name}
      {...(chart.better === null ? {} : {note: BETTER_WORDS[chart.better]})}
      {...(headline === undefined ? {} : {figure: headline.words, tone: headline.tone})}
      {...(headline === undefined || headline.hint === '' ? {} : {hint: headline.hint})}
      {...(chart.unit === '' ? {} : {unit: chart.unit})}
    >
      <Chart
        label={chart.label}
        points={chart.readings}
        steps={chart.steps}
        {...(chart.start === null ? {} : {start: chart.start})}
        references={chart.baselines}
        {...(chart.cursor === null ? {} : {cursor: chart.cursor})}
        marks={chart.resumes}
        markLabel="resumed"
        caption={chart.caption}
        xDomain={[0, chart.xEnd]}
        xTicks={chart.xTicks}
        yTicks={chart.yTicks}
        yDomain={chart.yDomain}
        pick={id => props.zoom({type: 'open-idea', idea: id})}
        {...(props.hover === undefined ? {} : {hover: props.hover})}
      />
    </ChartPanel>
  );
}

/**
 * The charts of the run, drawn from `whole`, not from the viewed model, so scrubbing neither mounts
 * nor unmounts a chart nor rescales its axes; the scrub position is the cursor of each, and the
 * headlines read the best as of it. `idea` is the open idea's id, or null.
 */
export function Hero(
  props: ViewBase & {
    readonly whole: RunModel;
    readonly idea: string | null;
    /** The ideas on the chain the reader points at, ringed on the charts; none when omitted. */
    readonly lit?: ReadonlySet<string>;
    /** Called with an idea's id as the pointer enters its reading, and null as it leaves. */
    readonly hover?: (idea: string | null) => void;
  },
): JSX.Element {
  const objectives = objectivesOf(props.whole);
  // The time bar stands before the run's end exactly when the viewed model is not the whole run.
  const scrubbed = props.model.at !== null && props.model.at !== props.whole.at;
  const scrub = scrubbed ? (props.model.latest ?? props.clock.start) : null;
  const hero = heroView(props.whole, objectives, scrub);
  const charts = objectiveCharts(
    props.whole,
    props.clock.start,
    props.clock.now,
    scrub,
    props.idea,
    props.lit,
  );
  const pareto = paretoView(props.whole, props.clock.start, scrub, props.idea, props.lit);
  const legend = chartLegend(charts);
  return (
    <Stack gap="space-3">
      <ChartColumns count={charts.length + (pareto === null ? 0 : 1)}>
        {charts.map((chart, at) => (
          <ObjectivePanel
            key={chart.id}
            chart={chart}
            headline={hero.headlines[at]}
            zoom={props.zoom}
            hover={props.hover}
          />
        ))}
        {pareto === null ? null : (
          <ChartPanel title="Trade-off" note="frontier of the best">
            <ParetoChart
              label={pareto.label}
              size="strip"
              points={pareto.points}
              x={pareto.x}
              y={pareto.y}
              highlighted={pareto.highlighted}
              {...(pareto.cursor === undefined ? {} : {cursor: pareto.cursor})}
              pick={id => {
                if (id !== 'input') props.zoom({type: 'open-idea', idea: id});
              }}
              {...(props.hover === undefined
                ? {}
                : {hover: (id: string | null) => props.hover?.(id === 'input' ? null : id)})}
            />
          </ChartPanel>
        )}
      </ChartColumns>
      {legend.length === 0 ? null : (
        <ChartLegend label="Chart key" items={legend.map(entry => LEGEND_WORDS[entry])} />
      )}
      <BaselineNotes notes={hero.baselineNotes} />
    </Stack>
  );
}
