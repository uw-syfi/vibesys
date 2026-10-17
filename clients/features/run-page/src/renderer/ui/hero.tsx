/**
 * The performance row: the hero (the run's best ratio to the input, large, with its value, the
 * input's and the metric) sharing a row with the strip chart. The Run screen draws it above the
 * level, so it is the same element at every level and navigating moves nothing in it: the panes
 * below change, this row does not. The figures' block has a fixed width, so a long title never
 * pushes the chart. With an idea open, the chart singles the idea's point out (the
 * others dimmed, none removed) and the block gains one line, reserved at every level so the block
 * never changes its size, that reads the idea's result.
 */
import {Button, Chart, Chip, Fact, FactLine, Row, Stack, Text} from '@vibesys/ui';
import type {JSX} from 'react';
import {
  heroView,
  type IdeaFocusView,
  ideaFocusView,
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
        <Fact title={view.title}>
          <Text variant="code" tone={view.tone}>
            {`this idea · ${view.outcome}`}
          </Text>
        </Fact>
      ) : (
        <>
          <Fact title={view.title}>
            <Text variant="code" tone={view.tone}>
              {`${view.ratio} vs input`}
            </Text>
          </Fact>
          <Fact shrink title={view.title}>
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
 * The figures at the viewed sequence, and the chart of the whole run. The chart is drawn from
 * `whole`, not from the viewed model, so scrubbing neither mounts nor unmounts it nor rescales its
 * axes; the scrub position is its cursor. `idea` is the open idea's id, or null.
 */
export function Hero(
  props: ViewBase & {
    readonly whole: RunModel;
    readonly idea: string | null;
    /** The ideas on the chain the reader points at, lit on the chart; none when omitted. */
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
    <Row gap="space-6" align="center">
      <Stack gap="space-1" width="figure">
        <Text variant="section">{hero.metric}</Text>
        {hero.standing === null || hero.bestIdea === null ? null : (
          <FactLine fit="parent" wrap={false}>
            <Fact>
              <Chip label={hero.standing} tone="ok" dot />
            </Fact>
            <Fact shrink title={hero.bestIdea.title}>
              <Button
                size="sm"
                variant="quiet"
                text={hero.bestIdea.title}
                press={() => {
                  if (hero.bestIdea !== null) {
                    props.zoom({type: 'open-idea', idea: hero.bestIdea.id});
                  }
                }}
              />
            </Fact>
          </FactLine>
        )}
        <Row gap="space-3" align="baseline">
          <Text variant="hero" tone={hero.ratio === null ? 'muted' : 'ok'}>
            {hero.ratio ?? '–'}
          </Text>
          <Stack gap="space-0">
            <Text variant="code">{hero.value ?? 'nothing measured yet'}</Text>
            {hero.input === null ? null : (
              <Text variant="code" tone="muted">
                {hero.input}
              </Text>
            )}
          </Stack>
        </Row>
        <IdeaLine view={ideaFocusView(props.model, props.idea)} />
      </Stack>
      {chart === null ? null : (
        <Stack gap="space-0" grow>
          <Chart
            label={chart.label}
            size="strip"
            points={chart.points}
            spans={chart.spans}
            edges={chart.edges}
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
