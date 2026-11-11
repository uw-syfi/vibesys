import {Card, ChartLegend, Table, type TableColumn, Text} from '@vibesys/ui';
import type {JSX} from 'react';
import type {LatestMeasurement, ObjectiveReport} from '../../../state/index.js';
import {shortCommit} from '../../commit-text.js';
import {useDisplayClock} from '../../display-clock.js';
import {formatAge, formatExact, formatFigure, withUnit} from './format.js';

/** What a row says of the idea its job measured: its plan numbers, `No plan`, or a dash while no ideas are served. */
function ideaWords(plans: readonly number[] | undefined): string {
  if (plans === undefined) return '—';
  return plans.length === 0 ? 'No plan' : plans.map(plan => `#${plan}`).join(' ');
}

/**
 * The newest measurements as a table: when, which idea, one value for each objective and the commit.
 * A value that set a new best is the accent's, which the key in the heading says. A run whose
 * measurements are over (it has ended) writes the time of day, since "how long ago" is of a moment
 * that no longer matters; a live one writes how long ago. Every value is the host's.
 */
export function LatestMeasurements(props: {
  readonly rows: readonly LatestMeasurement[];
  readonly objectives: readonly ObjectiveReport[];
  readonly nowMs: number;
  readonly runOver: boolean;
}): JSX.Element {
  const clock = useDisplayClock();
  const columns: TableColumn<LatestMeasurement>[] = [
    {
      key: 'when',
      header: 'When',
      mono: true,
      cell: row => (props.runOver ? clock.reportTime(row.atMs) : formatAge(props.nowMs - row.atMs)),
    },
    {key: 'idea', header: 'Idea', mono: true, cell: row => ideaWords(row.plans)},
    ...props.objectives.map(
      (objective, at): TableColumn<LatestMeasurement> => ({
        key: `value-${objective.name}`,
        header: objective.name,
        align: 'end',
        cell: row => {
          const value = row.values[at];
          if (value === null || value === undefined) return <Text tone="muted">—</Text>;
          return (
            <Text
              variant="code"
              tone={value.newBest ? 'accent' : 'default'}
              title={withUnit(formatExact(value.value), objective.unit)}
            >
              {formatFigure(value.value)}
            </Text>
          );
        },
      }),
    ),
    {key: 'commit', header: 'Commit', mono: true, cell: row => shortCommit(row.commit)},
  ];
  return (
    <Card
      dense
      section="Latest measurements"
      action={<ChartLegend label="Key to the values" items={[{label: 'New best', mark: 'line'}]} />}
    >
      <Table
        label="Latest measurements"
        dense
        middle
        rows={props.rows}
        rowKey={row => row.id}
        columns={columns}
        empty="No measurements yet"
      />
    </Card>
  );
}
