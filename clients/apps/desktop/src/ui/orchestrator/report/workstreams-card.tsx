import {WorkstreamStatus} from '@vibesys/orchestrator-proto';
import {
  Card,
  DotLegend,
  Fact,
  Row,
  ShareBar,
  Stack,
  Table,
  type TableColumn,
  Text,
} from '@vibesys/ui';
import type {JSX} from 'react';
import type {WorkstreamRow} from '../../../state/index.js';
import {formatGain} from './format.js';
import {VERDICT_TONES, VERDICT_WORDS} from './verdicts.js';
import {yet} from './yet.js';

const STATUS_WORDS = {
  [WorkstreamStatus.UNSPECIFIED]: '',
  [WorkstreamStatus.ACTIVE]: 'Active',
  [WorkstreamStatus.PAUSED]: 'Paused',
  [WorkstreamStatus.CLOSED]: 'Closed',
} as const satisfies Record<WorkstreamStatus, string>;

/** What a row's name is: its title, or the ideas that name no workstream. */
const nameOf = (row: WorkstreamRow): string =>
  row.number === 0 ? 'No workstream' : (row.title ?? `Workstream ${row.number}`);

/** What the pointer says of a row: its whole name, and its status and note when the host served them. */
function titleOf(row: WorkstreamRow): string {
  const status = row.status === undefined ? '' : STATUS_WORDS[row.status];
  return [nameOf(row), status, row.note].filter(part => part !== '').join(' · ');
}

const VERDICTS = ['kept', 'rejected', 'open', 'parked'] as const;

/** The ideas of a workstream as one bar in the verdicts' colors, and how many they are. */
function IdeasBar(props: {readonly row: WorkstreamRow}): JSX.Element {
  const {row} = props;
  const total = VERDICTS.reduce((sum, verdict) => sum + row[verdict], 0);
  return (
    <Row gap="space-3">
      <ShareBar
        bare
        label={`Ideas of ${nameOf(row)} by verdict`}
        parts={VERDICTS.map(verdict => ({
          key: verdict,
          label: VERDICT_WORDS[verdict],
          value: row[verdict],
          figure: String(row[verdict]),
          series: 0,
          tone: VERDICT_TONES[verdict],
        }))}
      />
      <Fact>
        <Text variant="code" tone="muted">
          {total}
        </Text>
      </Fact>
    </Row>
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
  /** The run has ended: no workstream is still to come. */
  readonly runOver: boolean;
}): JSX.Element {
  return (
    <Card dense section="Workstreams">
      <Stack gap="space-3">
        <DotLegend
          items={VERDICTS.map(verdict => ({
            tone: VERDICT_TONES[verdict],
            label: VERDICT_WORDS[verdict],
          }))}
        />
        <Table
          label="Workstream list"
          dense
          middle
          rows={props.workstreams}
          rowKey={row => String(row.number)}
          columns={COLUMNS}
          empty={yet('No workstreams', props.runOver)}
        />
      </Stack>
    </Card>
  );
}
