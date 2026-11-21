import {
  Card,
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
import type {ObjectiveReport, TradeoffReport} from '../../../state/index.js';
import {shortCommit} from '../../commit-text.js';

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
  // `chosen` survives a report whose objective set has changed under it (the same chart reused for
  // a different run, with no remount between): neither name may still be one of `names`. Falling
  // back to `Math.max(0, -1)` for each axis independently would then put both axes at index 0, the
  // same objective. Fall back to this report's own first two objectives instead, together, so a
  // stale pick never aliases the two axes to one.
  const picked =
    names.includes(chosen.x) && names.includes(chosen.y)
      ? chosen
      : {x: names[0] ?? '', y: names[1] ?? names[0] ?? ''};
  const xAt = names.indexOf(picked.x);
  const yAt = names.indexOf(picked.y);
  const units = new Map(props.objectives.map(objective => [objective.name, objective.unit]));
  const x = tradeoff.objectives[xAt];
  const y = tradeoff.objectives[yAt];
  const points: ParetoPoint[] = tradeoff.points.map(point => ({
    id: point.id,
    label: shortCommit(point.commit),
    x: point.values[xAt] ?? 0,
    y: point.values[yAt] ?? 0,
    time: point.atMs,
    state: 'passed',
    onFrontier: point.onFrontier,
  }));
  const options = names.map(name => ({value: name, label: name}));
  return (
    <Card dense section="Trade-off">
      <Stack gap="space-3">
        <Row gap="space-4" rowGap="space-2" wrap justify="between">
          <Text variant="small" tone="muted">
            {`${tradeoff.points.length} ${
              tradeoff.points.length === 1 ? 'candidate' : 'candidates'
            } · ${tradeoff.frontierCount} on the frontier`}
          </Text>
          {names.length > 2 ? (
            <Row gap="space-3" wrap>
              <Field label="X axis">
                <Select
                  value={names[xAt] ?? ''}
                  options={options}
                  change={name => choose({...picked, x: name})}
                  size="sm"
                />
              </Field>
              <Field label="Y axis">
                <Select
                  value={names[yAt] ?? ''}
                  options={options}
                  change={name => choose({...picked, y: name})}
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
