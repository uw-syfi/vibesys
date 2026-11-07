import {Card, ShareBar, Stack, SwatchLegend, Table, Text} from '@vibesys/ui';
import type {JSX} from 'react';
import {WorkstreamStatus} from '@vibesys/orchestrator-proto';
import type {WorkstreamRow} from '../../../state/index.js';
import {formatGain} from './format.js';

const STATUS_WORDS = {
  [WorkstreamStatus.UNSPECIFIED]: '',
  [WorkstreamStatus.ACTIVE]: 'Active',
  [WorkstreamStatus.PAUSED]: 'Paused',
  [WorkstreamStatus.CLOSED]: 'Closed',
} as const satisfies Record<WorkstreamStatus, string>;

/** The palette color of each verdict in the bars, and in the key above them. */
const SERIES = {kept: 2, rejected: 5, open: 1, parked: 7} as const;

/** What a row's name is: its title, or the ideas that name no workstream. */
const nameOf = (row: WorkstreamRow): string =>
  row.number === 0 ? 'No workstream' : (row.title ?? `Workstream ${row.number}`);

function IdeasBar(props: {readonly row: WorkstreamRow}): JSX.Element {
  const {row} = props;
  return (
    <ShareBar
      bare
      label={`Ideas of ${nameOf(row)} by verdict`}
      parts={(['kept', 'rejected', 'open', 'parked'] as const).map(verdict => ({
        key: verdict,
        label: `${verdict[0]?.toUpperCase()}${verdict.slice(1)}`,
        value: row[verdict],
        figure: String(row[verdict]),
        series: SERIES[verdict],
      }))}
    />
  );
}

/**
 * The run's workstreams: for each, the host's count of its ideas by verdict as one bar, how many
 * ideas that is and its best gain against the baseline. The first objective's gain is the column's;
 * the others are in the hover. Nothing here is counted or ranked by the app.
 */
export function WorkstreamsCard(props: {
  readonly workstreams: readonly WorkstreamRow[];
}): JSX.Element {
  return (
    <Card
      section="Workstreams"
      action={
        <SwatchLegend
          label="Key to the bars"
          items={[
            {label: 'Kept', series: SERIES.kept},
            {label: 'Rejected', series: SERIES.rejected},
            {label: 'Open', series: SERIES.open},
            {label: 'Parked', series: SERIES.parked},
          ]}
        />
      }
    >
      {props.workstreams.length === 0 ? (
        <Text tone="muted">No workstreams</Text>
      ) : (
        <Table
          label="Workstreams"
          rows={props.workstreams}
          rowKey={row => String(row.number)}
          middle
          columns={[
            {
              key: 'name',
              header: 'Name',
              lead: true,
              clip: nameOf,
              cell: row => (
                <Stack gap="space-1">
                  <Text>{nameOf(row)}</Text>
                  {row.status === undefined ||
                  row.status === WorkstreamStatus.UNSPECIFIED ? null : (
                    <Text variant="small" tone="muted" title={row.note}>
                      {STATUS_WORDS[row.status]}
                    </Text>
                  )}
                </Stack>
              ),
            },
            {key: 'ideas', header: 'Ideas', cell: row => <IdeasBar row={row} />},
            {
              key: 'n',
              header: 'N',
              mono: true,
              align: 'end',
              cell: row => String(row.kept + row.rejected + row.open + row.parked),
            },
            {
              key: 'gain',
              header: 'Gain',
              align: 'end',
              cell: row => {
                const first = row.gains[0];
                return first === undefined ? (
                  <Text tone="muted">—</Text>
                ) : (
                  <Text
                    variant="code"
                    tone={first.ratio > 1 ? 'ok' : 'muted'}
                    title={row.gains
                      .map(gain => `${gain.metric} ${formatGain(gain.ratio)}`)
                      .join('\n')}
                  >
                    {formatGain(first.ratio)}
                  </Text>
                );
              },
            },
          ]}
        />
      )}
    </Card>
  );
}
