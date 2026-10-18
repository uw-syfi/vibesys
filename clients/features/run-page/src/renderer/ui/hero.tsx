/**
 * The performance row: a tile per performance measure (its best ratio to the input, large, with
 * what it means, the value and the input's, and a small trend) sharing a row with the strip chart.
 * The Run screen draws it above the level, so it is the same element at every level and navigating
 * moves nothing in it: the panes below change, this row does not. The tiles' block has the width of
 * its tiles, so a long title never pushes the chart. With an idea open, the chart singles the
 * idea's point out (the others dimmed, none removed) and the block gains one line, reserved at
 * every level so the block never changes its size, that reads the idea's result.
 */
import {
  Button,
  Chart,
  Fact,
  FactLine,
  MetricTile,
  MetricTiles,
  markdownToPlain,
  Reserve,
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
      {view.ratio === null || view.value === null ? (
        <Fact title={markdownToPlain(view.title)}>
          <Text variant="code" tone={view.tone}>
            {`this idea · ${view.outcome}`}
          </Text>
        </Fact>
      ) : (
        <>
          <Fact title={markdownToPlain(view.title)}>
            <Text variant="code" tone={view.tone}>
              {`${view.ratio} vs input`}
            </Text>
          </Fact>
          <Fact shrink title={markdownToPlain(view.title)}>
            <Text variant="code" tone="muted">
              {`this idea · ${view.value}`}
            </Text>
          </Fact>
        </>
      )}
    </FactLine>
  );
}

/**
 * The line under the tiles that names which idea the figures belong to: `Adopted: <idea>` once the
 * run ended, `Best so far: <idea>` while it works; the idea's name opens it. Before there is one
 * the line is drawn empty and kept (`Reserve`), so the tiles above it, and the chart beside or
 * below, stay where they are when the first idea passes or the run is scrubbed to the start.
 */
function StandingLine(props: {
  readonly standing: 'best' | 'adopted' | null;
  readonly idea: {readonly id: string; readonly title: string} | null;
  readonly open: (idea: string) => void;
}): JSX.Element {
  const {standing, idea} = props;
  const shown = standing !== null && idea !== null;
  const title = shown ? markdownToPlain(idea.title) : '–';
  const line = (
    <FactLine fit="parent" wrap={false}>
      <Fact>
        <Text variant="small" tone="muted">
          {standing === 'adopted' ? 'Adopted:' : 'Best so far:'}
        </Text>
      </Fact>
      <Fact shrink title={title}>
        <Button
          size="sm"
          variant="quiet"
          text={title}
          press={() => {
            if (shown) props.open(idea.id);
          }}
        />
      </Fact>
    </FactLine>
  );
  return shown ? line : <Reserve>{line}</Reserve>;
}

/** One tile: the measure's name, its ratio to the input and what it means, its values, its trend. */
function Tile(props: {readonly tile: MetricTileView}): JSX.Element {
  const {tile} = props;
  return (
    <MetricTile
      name={tile.name}
      figure={tile.ratio ?? '–'}
      {...(tile.note === null ? {} : {note: tile.note})}
      tone={tile.tone}
      detail={tile.detail}
      trend={
        tile.trend === null ? null : (
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
  const hero = heroView(props.model);
  const scrub = props.model.at === null ? null : (props.model.latest ?? props.clock.start);
  const chart = stripChartView(
    props.whole,
    props.clock.start,
    props.clock.now,
    scrub,
    props.idea,
    props.lit,
  );
  return (
    <Row gap="space-6" align="center" wrap>
      <Stack gap="space-2">
        <MetricTiles label="Performance" count={hero.tiles.length}>
          {hero.tiles.map(tile => (
            <Tile key={tile.id} tile={tile} />
          ))}
        </MetricTiles>
        <StandingLine
          standing={hero.standing}
          idea={hero.bestIdea}
          open={id => props.zoom({type: 'open-idea', idea: id})}
        />
        <IdeaLine view={ideaFocusView(props.model, props.idea)} />
      </Stack>
      {chart === null ? null : (
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
  );
}
