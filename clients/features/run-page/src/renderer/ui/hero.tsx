/**
 * The performance row: a chart per objective of the run, each under its name and its headline (the
 * best so far as a multiple of the input), side by side in rows of at most three, each as tall as the
 * others (`chartsAcross`); with exactly two objectives the trade-off chart (its points and the
 * frontier of the best trade-offs) stands beside them. The stats and the key to what the charts draw
 * go on one line above them, which the screen draws under the time bar. Each chart is the best so far
 * as one line over a dot for each reading, on the run's running time; the time bar's position is a
 * line on every chart, the dots and the line after it dimmed. With an idea open, its reading is ringed and the steps its lineage set are ringed
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
  Row,
  Stack,
  Text,
} from '@vibesys/ui';
import type {JSX} from 'react';
import {
  type ChartLegendEntry,
  chartLegend,
  chartsAcross,
  type HeroView,
  heroView,
  inLastChartRow,
  type ObjectiveChartView,
  type ObjectiveHeadline,
  objectiveCharts,
  objectivesOf,
  paretoView,
  type RunPosition,
} from '../../state/index.js';
import {RunStats} from './run-stats.js';
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

/** What a chart's heading says of which way is better, and the same in fewer words for a crowded row. */
const BETTER_WORDS = {higher: 'higher is better', lower: 'lower is better'} as const;
const BETTER_SHORT = {higher: 'higher better', lower: 'lower better'} as const;

/** One objective: its heading with the headline, and its chart. */
function ObjectivePanel(props: {
  readonly chart: ObjectiveChartView;
  readonly headline: ObjectiveHeadline | undefined;
  readonly zoom: ViewBase['zoom'];
  readonly hover: ((idea: string | null) => void) | undefined;
  /** Four or more charts: the heading's note is the short one. */
  readonly crowded: boolean;
  /** The chart is in the last row: only those say what the x axis measures. */
  readonly last: boolean;
}): JSX.Element {
  const {chart, headline} = props;
  const words = props.crowded ? BETTER_SHORT : BETTER_WORDS;
  return (
    <ChartPanel
      title={headline?.name ?? chart.name}
      oneLine
      {...(chart.better === null ? {} : {note: words[chart.better]})}
      {...(headline === undefined || headline.words === ''
        ? {}
        : {figure: headline.words, tone: headline.tone})}
      {...(headline === undefined || headline.hint === '' ? {} : {hint: headline.hint})}
    >
      <Chart
        label={chart.label}
        size="compact"
        {...(chart.unit === '' ? {} : {unit: chart.unit})}
        points={chart.readings}
        steps={chart.steps}
        {...(chart.start === null ? {} : {start: chart.start})}
        references={chart.baselines}
        {...(chart.cursor === null ? {} : {cursor: chart.cursor})}
        marks={chart.resumes}
        markLabel="resumed"
        {...(props.last ? {caption: chart.caption} : {})}
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

/** The performance section's parts, for the screen to place: the stats and key above, the charts below. */
export interface HeroParts {
  /** The run's figures on one line. */
  readonly stats: JSX.Element;
  /** The key to what the charts draw; null for a run with nothing measured. */
  readonly legend: JSX.Element | null;
  readonly charts: JSX.Element;
  /** The comparisons with no reading. */
  readonly notes: JSX.Element | null;
}

export type HeroProps = ViewBase & {
  readonly position: RunPosition;
  readonly idea: string | null;
  /** The ideas on the chain the reader points at, ringed on the charts; none when omitted. */
  readonly lit?: ReadonlySet<string>;
  /** Called with an idea's id as the pointer enters its reading, and null as it leaves. */
  readonly hover?: (idea: string | null) => void;
};

/**
 * The charts of the run, drawn from the run to its end (`position.whole`), not from the viewed
 * model, so scrubbing neither mounts nor unmounts a chart nor rescales its axes; the scrub position
 * is the cursor of each, and the headlines and the stats read the run as of it. `idea` is the open
 * idea's id, or null.
 */
export function heroParts(props: HeroProps): HeroParts {
  const {whole, scrub} = props.position;
  const hero = heroView(whole, objectivesOf(whole), scrub);
  const charts = objectiveCharts(whole, {
    start: props.clock.start,
    now: props.clock.now,
    scrub,
    focus: props.idea,
    ...(props.lit === undefined ? {} : {lit: props.lit}),
  });
  const pareto = paretoView(whole, props.clock.start, scrub, props.idea, props.lit);
  const legend = chartLegend(charts);
  const panels = charts.length + (pareto === null ? 0 : 1);
  return {
    stats: (
      <RunStats
        model={props.model}
        position={props.position}
        start={props.clock.start}
        ledger={props.tokens}
      />
    ),
    legend:
      legend.length === 0 ? null : (
        <ChartLegend label="Chart key" items={legend.map(entry => LEGEND_WORDS[entry])} />
      ),
    charts: (
      <ChartColumns across={chartsAcross(panels)}>
        {charts.map((chart, at) => (
          <ObjectivePanel
            key={chart.id}
            chart={chart}
            headline={hero.headlines[at]}
            zoom={props.zoom}
            hover={props.hover}
            crowded={panels > 3}
            last={inLastChartRow(at, panels)}
          />
        ))}
        {pareto === null ? null : (
          <ChartPanel title="Trade-off" note="frontier of the best" oneLine>
            <ParetoChart
              label={pareto.label}
              size="compact"
              legend={false}
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
    ),
    notes: hero.baselineNotes.length === 0 ? null : <BaselineNotes notes={hero.baselineNotes} />,
  };
}

/** The whole performance section in one stack: the stats and key, the charts, the notes. */
export function Hero(props: HeroProps): JSX.Element {
  const parts = heroParts(props);
  return (
    <Stack gap="space-3" apart>
      <Row justify="between" align="baseline" wrap>
        {parts.stats}
        {parts.legend}
      </Row>
      {parts.charts}
      {parts.notes}
    </Stack>
  );
}
