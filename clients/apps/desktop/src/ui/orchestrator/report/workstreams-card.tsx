import {WorkstreamStatus} from '@vibesys/orchestrator-proto';
import {Card, ShareBar, Stack, SwatchLegend, Table, type TableColumn, Text} from '@vibesys/ui';
import type {JSX} from 'react';
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

/** What the pointer says of a row: its whole name, and its status and note when the host served them. */
function titleOf(row: WorkstreamRow): string {
  const status = row.status === undefined ? '' : STATUS_WORDS[row.status];
  return [nameOf(row), status, row.note].filter(part => part !== '').join(' · ');
}

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

const COLUMNS: TableColumn<WorkstreamRow>[] = [
  {key: 'name', header: 'Workstream', lead: true, clip: titleOf, cell: nameOf},
  {key: 'ideas', header: 'Ideas', cell: row => <IdeasBar row={row} />},
  {
    key: 'gain',
    header: 'Gain',
    mono: true,
    align: 'end',
    cell: row => {
      const first = row.gains[0];
      return first === undefined ? (
        <Text tone="muted">—</Text>
      ) : (
        <Text
          variant="code"
          tone={first.ratio > 1 ? 'ok' : 'muted'}
          title={row.gains.map(gain => `${gain.metric} ${formatGain(gain.ratio)}`).join('\n')}
        >
          {formatGain(first.ratio)}
        </Text>
      );
    },
  },
];

/**
 * The run's workstreams as compact rows: the name, the host's count of its ideas by verdict as one
 * bar (named by the key above), and the best gain against the baseline. The first objective's gain is
 * the column's; the others are in the hover. Nothing here is counted or ranked by the app.
 */
export function WorkstreamsCard(props: {
  readonly workstreams: readonly WorkstreamRow[];
}): JSX.Element {
  return (
    <Card dense section="Workstreams">
      <Stack gap="space-3">
        <SwatchLegend
          label="Key to the bars"
          items={[
            {label: 'Kept', series: SERIES.kept},
            {label: 'Rejected', series: SERIES.rejected},
            {label: 'Open', series: SERIES.open},
            {label: 'Parked', series: SERIES.parked},
          ]}
        />
        <Table
          label="Workstream list"
          dense
          middle
          rows={props.workstreams}
          rowKey={row => String(row.number)}
          columns={COLUMNS}
          empty="No workstreams yet"
        />
      </Stack>
    </Card>
  );
}
