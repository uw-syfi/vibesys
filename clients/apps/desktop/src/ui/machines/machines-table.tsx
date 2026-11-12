import {Button, Card, Chip, Row, Stale, Table, type TableColumn, Text} from '@vibesys/ui';
import type {JSX} from 'react';
import {type MachineTableRow, machineKey, type Reach} from '../../state/index.js';
import {HostStatusChip} from '../host-status-chip.js';
import {offerWords} from '../orchestrator/new-run/words.js';
import {Reading, ReadingReserve, readingSpan} from './machine-cells.js';

const REACH_TONE: Readonly<Record<Reach, 'ok' | 'err' | 'neutral'>> = {
  reachable: 'neutral',
  unreachable: 'err',
  'not probed': 'neutral',
};

/** A place's reach as a status word, in the capitals every status chip has. */
const REACH_WORD: Readonly<Record<Reach, string>> = {
  reachable: 'Reachable',
  unreachable: 'Unreachable',
  'not probed': 'Not probed',
};

const NOTE_WORDS = {
  waiting: 'Waiting for job places',
  none: 'No job places',
  'not-read': 'Job places not read',
} as const;

/** The actions of the machines, each present only when the window can do it. */
export interface MachineRowActions {
  /** The host probes this machine's places again. */
  readonly refresh?: (key: string) => void;
  readonly remove?: (key: string) => void;
}

const offersOf = (row: Extract<MachineTableRow, {readonly kind: 'place'}>): string => {
  if (row.reach !== 'reachable') return '';
  const {offers} = row.place;
  return offers.length === 0 ? 'CPU only' : offers.map(offerWords).join(', ');
};

/** The whole text of a clipped name or note cell, which remains in the document and on hover. */
const nameText = (row: MachineTableRow): string => {
  switch (row.kind) {
    case 'machine':
      return row.entry.row.label;
    case 'place':
      return row.place.name;
    case 'note':
      return NOTE_WORDS[row.note];
  }
};

function NameCell(props: {readonly row: MachineTableRow}): JSX.Element {
  const {row} = props;
  switch (row.kind) {
    case 'machine':
      return <Text variant="item">{row.entry.row.label}</Text>;
    case 'place':
      return (
        <Stale stale={row.dim}>
          <Text variant="code">{row.place.name}</Text>
        </Stale>
      );
    case 'note':
      return (
        <Text variant="small" tone="muted">
          {NOTE_WORDS[row.note]}
        </Text>
      );
  }
}

function StatusCell(props: {readonly row: MachineTableRow}): JSX.Element | null {
  const {row} = props;
  switch (row.kind) {
    case 'machine':
      return <HostStatusChip status={row.entry.row.status} />;
    case 'place':
      return (
        <Stale stale={row.dim}>
          <Chip
            label={REACH_WORD[row.reach]}
            tone={REACH_TONE[row.reach]}
            {...(row.detail === '' ? {} : {hint: row.detail})}
          />
        </Stale>
      );
    case 'note':
      return row.note === 'not-read' ? (
        <Chip label="Not read" tone="warn" {...(row.failure === null ? {} : {hint: row.failure})} />
      ) : null;
  }
}

/** The GPU cell of a place is what it offers; the machine's is its GPUs' reading. */
function GpuCell(props: {readonly row: MachineTableRow}): JSX.Element | null {
  const {row} = props;
  switch (row.kind) {
    case 'machine':
      return <Reading view={row.entry.stats} which="gpu" />;
    case 'place':
      return (
        <Stale stale={row.dim}>
          <Text variant="small" tone="muted">
            {offersOf(row)}
          </Text>
        </Stale>
      );
    case 'note':
      return null;
  }
}

function ActionsCell(props: {
  readonly row: MachineTableRow;
  readonly actions: MachineRowActions;
}): JSX.Element | null {
  const {row, actions} = props;
  if (row.kind !== 'machine') return null;
  const {key, label} = row.entry.row;
  const reading = row.entry.places?.reading === true;
  return (
    <Row gap="space-1">
      {actions.refresh === undefined || row.entry.places === null ? null : (
        <Button
          variant="quiet"
          size="sm"
          icon="refresh"
          label={`Refresh job places on ${label}`}
          hint={`Refresh job places on ${label}`}
          press={() => actions.refresh?.(key)}
          {...(reading ? {disabledReason: 'Reading', quietReason: true} : {})}
        />
      )}
      {actions.remove === undefined || key === 'local' ? null : (
        <Button
          variant="quiet"
          size="sm"
          icon="cross"
          label={`Remove ${label} from this app`}
          hint={`Remove ${label} from this app`}
          press={() => actions.remove?.(key)}
        />
      )}
    </Row>
  );
}

/** Whether any machine has an action, so a window that only lists draws no column for them. */
const hasActions = (actions: MachineRowActions): boolean =>
  actions.refresh !== undefined || actions.remove !== undefined;

function columnsOf(
  actions: MachineRowActions,
  readings: boolean,
  places: boolean,
): readonly TableColumn<MachineTableRow>[] {
  const columns: TableColumn<MachineTableRow>[] = [
    {
      key: 'name',
      header: 'Name',
      lead: true,
      clip: nameText,
      cell: row => <NameCell row={row} />,
    },
    {key: 'status', header: 'Status', fit: true, cell: row => <StatusCell row={row} />},
  ];
  // A window that reads no machine stats has no CPU or memory to put in a column; its places still
  // say what they offer in the GPU column, which is then the column of what a place gives.
  if (readings) {
    columns.push(
      {
        key: 'cpu',
        header: 'CPU',
        fit: true,
        reserve: <ReadingReserve which="cpu" />,
        span: row => (row.kind === 'machine' ? readingSpan(row.entry.stats) : 1),
        cell: row =>
          row.kind === 'machine' ? <Reading view={row.entry.stats} which="cpu" /> : null,
      },
      {
        key: 'memory',
        header: 'Memory',
        fit: true,
        reserve: <ReadingReserve which="memory" />,
        cell: row =>
          row.kind === 'machine' ? <Reading view={row.entry.stats} which="memory" /> : null,
      },
    );
  }
  if (readings || places) {
    columns.push({
      key: 'gpu',
      header: 'GPU',
      fit: true,
      ...(readings ? {reserve: <ReadingReserve which="gpu" />} : {}),
      cell: row => <GpuCell row={row} />,
    });
  }
  if (hasActions(actions)) {
    columns.push({
      key: 'actions',
      header: 'Actions',
      fit: true,
      control: true,
      cell: row => <ActionsCell row={row} actions={actions} />,
    });
  }
  return columns;
}

/**
 * Every machine and the places jobs can run at, as one table: a row for each machine with its
 * status and readings, and under it a row for each of its places (named in code, with whether it
 * could be reached and what it offers). Places are rows beneath their machine, so they cannot be picked.
 */
export function MachinesTable(props: {
  readonly rows: readonly MachineTableRow[];
  readonly actions: MachineRowActions;
  /** The machines' readings are read: CPU, memory and GPU have columns. */
  readonly readings: boolean;
  /** The machines' places are read: the GPU column also says what a place offers. */
  readonly places: boolean;
  /** The host key of the row marked current (the machine just added). */
  readonly selected: string | null;
  readonly select: ((key: string) => void) | undefined;
}): JSX.Element {
  const {select} = props;
  return (
    <Card dense>
      <Table
        label="Machines"
        dense
        grouped
        middle
        empty="No machines"
        columns={columnsOf(props.actions, props.readings, props.places)}
        rows={props.rows}
        rowKey={row => row.key}
        rowKind={row => (row.kind === 'machine' ? undefined : 'sub')}
        {...(props.selected === null ? {} : {selected: machineKey(props.selected)})}
        {...(select === undefined
          ? {}
          : {
              select: (key: string) => {
                const row = props.rows.find(each => each.key === key);
                if (row?.kind === 'machine') select(row.entry.row.key);
              },
            })}
      />
    </Card>
  );
}
