import {
  Card,
  Chart,
  ChartLegend,
  ChartPanel,
  type ChartPoint,
  type ChartReference,
  type ChartTick,
  Field,
  type ParetoAxis,
  ParetoChart,
  type ParetoPoint,
  Row,
  Select,
  Stack,
  Text,
} from '@vibesys/ui';
import {type JSX, useState} from 'react';
import {
  type ObjectiveReport,
  type RunReport,
  type TradeoffReport,
  timeTicks,
  valueTicks,
} from '../../../state/index.js';
import {
  axisMoment,
  clip,
  formatExact,
  formatFigure,
  seattleMoment,
  seattleOffsetMs,
  withUnit,
} from './format.js';

/** The longest note of a bound a hover writes whole; the agent's own words go on after it in its store. */
const NOTE_LIMIT = 600;

/** How many labels a time axis aims for. */
const X_LABELS = 4;
const Y_LABELS = 3;
/** The share of the value range left above and below the data, so no point sits on the frame. */
const PAD = 0.1;

function boundReference(
  objective: ObjectiveReport,
  names: ReadonlyMap<string, string>,
): ChartReference | undefined {
  const bound = objective.bound?.current;
  if (bound === undefined) return undefined;
  const who = [
    names.get(bound.agent) ?? bound.agent,
    bound.atMs === null ? '' : seattleMoment(bound.atMs),
  ]
    .filter(part => part !== '')
    .join(' · ');
  return {
    y: bound.value,
    label: `Bound ${formatFigure(bound.value)}`,
    hint: [withUnit(formatExact(bound.value), bound.unit), clip(bound.note, NOTE_LIMIT), who]
      .filter(line => line !== '')
      .join('\n'),
  };
}

/** The range of the value axis: the measured values and the bound, a little widened. */
function valueDomain(objective: ObjectiveReport): readonly [number, number] {
  const values = [
    ...objective.points.map(point => point.value),
    ...(objective.bound === undefined ? [] : [objective.bound.current.value]),
  ];
  const low = Math.min(...values);
  const high = Math.max(...values);
  const room = (high - low || Math.abs(high) || 1) * PAD;
  return [low - room, high + room];
}

/** The labels of a value axis: the ticks over the objective's range, in figures. */
function valueLabels(objective: ObjectiveReport): ChartTick[] {
  const [low, high] = valueDomain(objective);
  return valueTicks(low, high, Y_LABELS).map(value => ({value, label: formatFigure(value)}));
}

function ObjectiveChart(props: {
  readonly objective: ObjectiveReport;
  readonly span: RunReport['span'];
  readonly size: 'default' | 'compact';
  readonly names: ReadonlyMap<string, string>;
  /** The widest value label of any chart on this axis, so every plot starts at one left edge. */
  readonly labelChars: number;
}): JSX.Element | null {
  const {objective, span} = props;
  if (objective.points.length === 0) return null;
  const length = span.toMs - span.fromMs;
  const domain = valueDomain(objective);
  const points: ChartPoint[] = objective.points.map(point => ({
    key: point.id,
    id: point.id,
    x: point.atMs,
    y: point.value,
    tip: {
      lead: point.commit.slice(0, 8),
      detail: withUnit(formatExact(point.value), objective.unit),
    },
  }));
  const last = objective.bestPath.at(-1);
  const steps = [
    ...objective.bestPath.map(point => ({x: point.atMs, y: point.value})),
    ...(last === undefined ? [] : [{x: Math.max(span.toMs, last.atMs), y: last.value}]),
  ];
  const xTicks: ChartTick[] = timeTicks(span.fromMs, span.toMs, X_LABELS, seattleOffsetMs).map(
    value => ({value, label: axisMoment(value, length)}),
  );
  const yTicks = valueLabels(objective);
  const reference = boundReference(objective, props.names);
  const best =
    objective.bestValue === undefined
      ? undefined
      : withUnit(formatFigure(objective.bestValue), objective.unit);
  return (
    <ChartPanel
      title={objective.name}
      note={objective.better === 'lower' ? 'Lower is better' : 'Higher is better'}
      oneLine
      {...(best === undefined ? {} : {figure: best, tone: 'muted' as const})}
    >
      <Chart
        label={`${objective.name} of every measured candidate over time, with the best so far`}
        points={points}
        steps={steps}
        xDomain={[span.fromMs, Math.max(span.toMs, span.fromMs + 1)]}
        yDomain={domain}
        xTicks={xTicks}
        yTicks={yTicks}
        yLabelChars={props.labelChars}
        size={props.size}
        {...(reference === undefined ? {} : {references: [reference]})}
      />
    </ChartPanel>
  );
}

/** Every objective over time, one chart each, on one time axis. */
export function OverTime(props: {
  readonly objectives: readonly ObjectiveReport[];
  readonly span: RunReport['span'];
  /** What each agent is called on screen, by agent id. */
  readonly names: ReadonlyMap<string, string>;
}): JSX.Element {
  const measured = props.objectives.filter(objective => objective.points.length > 0);
  const labelChars = Math.max(
    0,
    ...measured.flatMap(objective => valueLabels(objective).map(tick => tick.label.length)),
  );
  return (
    <Card section="Over time">
      {measured.length === 0 ? (
        <Text tone="muted">No measurements yet</Text>
      ) : (
        <Stack gap="space-5">
          <ChartLegend
            label="Key to the charts"
            items={[
              {label: 'Best so far', mark: 'line'},
              {label: 'Measured', mark: 'dot'},
            ]}
          />
          {measured.map(objective => (
            <ObjectiveChart
              key={objective.name}
              objective={objective}
              span={props.span}
              names={props.names}
              labelChars={labelChars}
              size={measured.length === 1 ? 'default' : 'compact'}
            />
          ))}
        </Stack>
      )}
    </Card>
  );
}

function axisOf(
  objective: TradeoffReport['objectives'][number],
  units: ReadonlyMap<string, string | null>,
): ParetoAxis {
  return {
    name: objective.name,
    unit: units.get(objective.name) ?? '',
    direction: objective.better,
  };
}

/**
 * Every candidate measured on all the objectives, two of them at a time (the person picks the pair),
 * with the frontier as the host marked it. With exactly two objectives the frontier is also joined;
 * with more it is marked on any pair and joined on none.
 */
export function TradeOff(props: {
  readonly tradeoff: TradeoffReport;
  readonly objectives: readonly ObjectiveReport[];
}): JSX.Element {
  const {tradeoff} = props;
  const [chosen, choose] = useState<{x: string; y: string}>({
    x: tradeoff.objectives[0]?.name ?? '',
    y: tradeoff.objectives[1]?.name ?? '',
  });
  const names = tradeoff.objectives.map(objective => objective.name);
  const xAt = Math.max(0, names.indexOf(chosen.x));
  const yAt = Math.max(0, names.indexOf(chosen.y));
  const units = new Map(props.objectives.map(objective => [objective.name, objective.unit]));
  const x = tradeoff.objectives[xAt];
  const y = tradeoff.objectives[yAt];
  const points: ParetoPoint[] = tradeoff.points.map(point => ({
    id: point.id,
    label: point.commit.slice(0, 8),
    x: point.values[xAt] ?? 0,
    y: point.values[yAt] ?? 0,
    time: point.atMs,
    state: 'passed',
    onFrontier: point.onFrontier,
  }));
  const options = names.map(name => ({value: name, label: name}));
  return (
    <Card section="Trade-off">
      <Stack gap="space-4">
        <Row gap="space-4" rowGap="space-2" wrap justify="between">
          <Text variant="small" tone="muted">
            {`${tradeoff.points.length} candidates · ${tradeoff.frontierCount} on the frontier`}
          </Text>
          {names.length > 2 ? (
            <Row gap="space-3" wrap>
              <Field label="X axis">
                <Select
                  value={names[xAt] ?? ''}
                  options={options}
                  change={name => choose({...chosen, x: name})}
                  size="sm"
                />
              </Field>
              <Field label="Y axis">
                <Select
                  value={names[yAt] ?? ''}
                  options={options}
                  change={name => choose({...chosen, y: name})}
                  size="sm"
                />
              </Field>
            </Row>
          ) : null}
        </Row>
        {x === undefined || y === undefined ? null : (
          <ParetoChart
            label="Every measured candidate on two objectives"
            points={points}
            x={axisOf(x, units)}
            y={axisOf(y, units)}
            joined={names.length === 2}
          />
        )}
      </Stack>
    </Card>
  );
}
