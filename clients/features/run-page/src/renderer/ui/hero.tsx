/**
 * The performance row: a tile per performance measure (its best ratio to the input, large, with
 * what it means, the value and the input's, and a small trend) sharing a row with the strip chart,
 * and under both, across the whole width, the line that reads an open idea's result.
 * The Run screen draws it above the level, so it is the same element at every level and navigating
 * moves nothing in it: the panes below change, this row does not. The lines are as wide as the row,
 * so a long idea name is cut only where the row ends, and never pushes the chart. With an idea open, the chart singles the
 * idea's point out (the others dimmed, none removed) and the block gains one line, reserved at
 * every level so the block never changes its size, that reads the idea's result. A run with two
 * objectives has the trade-off chart (its points and the frontier of the best trade-offs, at the
 * time bar's position) in the chart's place, the same size at every level.
 */
import {
  Chart,
  Fact,
  FactLine,
  MetricTile,
  MetricTiles,
  markdownToPlain,
  ParetoChart,
  Row,
  Sparkline,
  Stack,
  Text,
} from '@vibesys/ui';
import type {JSX} from 'react';
import {
  heroView,
  type IdeaFocusView,
  ideaFocusView,
  type MetricTileView,
  objectivesOf,
  paretoView,
  type RunModel,
  stripChartView,
} from '../../state/index.js';
import type {ViewBase} from './views/props.js';

/** A line high as the one that tells an open idea's result, empty when none is open. */
function IdeaLine(props: {readonly view: IdeaFocusView | null}): JSX.Element {
  const {view} = props;
  if (view === null) {
    return (
      <div aria-hidden="true">
        <FactLine fit="parent" wrap={false}>
          <Fact>
            <Text variant="code">{' '}</Text>
          </Fact>
        </FactLine>
      </div>
    );
  }
  return (
    <FactLine fit="parent" wrap={false}>
      <Fact>
        <Text variant="small" tone="muted">
          Selected idea:
        </Text>
      </Fact>
      <Fact shrink title={markdownToPlain(view.title)}>
        <Text variant="code">{markdownToPlain(view.title)}</Text>
      </Fact>
      {view.ratio === null || view.value === null ? (
        <Fact title={markdownToPlain(view.title)}>
          <Text variant="code" tone={view.tone}>
            {view.outcome}
          </Text>
        </Fact>
      ) : (
        <>
          <Fact title={markdownToPlain(view.title)}>
            <Text variant="code" tone={view.tone}>
              {`${view.ratio} vs ${view.base ?? 'input'}`}
            </Text>
          </Fact>
          <Fact shrink title={markdownToPlain(view.title)}>
            <Text variant="code" tone="muted">
              {view.value}
            </Text>
          </Fact>
        </>
      )}
    </FactLine>
  );
}

/**
 * One tile: the measure's name, its ratio to the input and what it means, its values, its trend.
 * Beside a trade-off chart, which already shows how the run advanced, a tile has no trend.
 */
function Tile(props: {readonly tile: MetricTileView; readonly trend: boolean}): JSX.Element {
  const {tile} = props;
  return (
    <MetricTile
      name={tile.name}
      figure={tile.figure}
      {...(tile.note === null ? {} : {note: tile.note})}
      tone={tile.tone}
      detail={tile.detail}
      hint={tile.hint}
      trend={
        tile.trend === null || !props.trend ? null : (
          <Sparkline
            label={tile.trend.label}
            steps={tile.trend.steps}
            from={tile.trend.from}
            to={tile.trend.to}
            {...(tile.trend.reference === null ? {} : {reference: tile.trend.reference})}
          />
        )
      }
    />
  );
}

/**
 * The figures at the viewed sequence, and the chart of the whole run. The chart is drawn from
 * `whole`, not from the viewed model, so scrubbing neither mounts nor unmounts it nor rescales its
 * axes; the scrub position is its cursor. `idea` is the open idea's id, or null.
 */
export function Hero(
  props: ViewBase & {
    readonly whole: RunModel;
    readonly idea: string | null;
    /** The ideas on the chain the reader points at, ringed on the chart; none when omitted. */
    readonly lit?: ReadonlySet<string>;
    /** Called with an idea's id as the pointer enters its point, and null as it leaves. */
    readonly hover?: (idea: string | null) => void;
  },
): JSX.Element {
  const objectives = objectivesOf(props.whole);
  const hero = heroView(props.model, objectives);
  const scrub = props.model.at === null ? null : (props.model.latest ?? props.clock.start);
  const pareto = paretoView(props.whole, props.clock.start, scrub, props.idea, props.lit);
  const chart = stripChartView(
    props.whole,
    props.clock.start,
    props.clock.now,
    scrub,
    props.idea,
    props.lit,
  );
  return (
    <Stack gap="space-2">
      <Row gap="space-6" align="center" wrap>
        <MetricTiles
          label="Performance"
          count={hero.tiles.length}
          stacked={objectives.length === 2}
        >
          {hero.tiles.map(tile => (
            <Tile key={tile.id} tile={tile} trend={objectives.length !== 2} />
          ))}
        </MetricTiles>
        {pareto === null ? null : (
          <Stack gap="space-0" width="chart">
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
          </Stack>
        )}
        {pareto !== null || chart === null ? null : (
          <Stack gap="space-0" width="chart">
            <Chart
              label={chart.label}
              size="strip"
              points={chart.points}
              spans={chart.spans}
              yDomain={chart.yDomain}
              steps={chart.steps}
              {...(chart.reference === null ? {} : {reference: chart.reference})}
              {...(chart.playhead === null ? {} : {playhead: chart.playhead})}
              xTicks={chart.xTicks}
              yTicks={chart.yTicks}
              pick={id => props.zoom({type: 'open-idea', idea: id})}
              {...(props.hover === undefined ? {} : {hover: props.hover})}
            />
          </Stack>
        )}
      </Row>
      <IdeaLine view={ideaFocusView(props.model, props.idea)} />
    </Stack>
  );
}
