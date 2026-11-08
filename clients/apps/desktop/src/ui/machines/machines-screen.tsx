import {Button, Page, Stack, Table, type TableColumn} from '@vibesys/ui';
import type {JSX} from 'react';
import type {MachineRow} from '../../state/index.js';
import {HostStatusChip} from '../host-status-chip.js';
import type {MachineControls} from './add-machine/machine-controls.js';
import {type MachineStatsHost, MachineStatsPanel} from './machine-stats-panel.js';
import {type PlacesHost, PlacesPanel} from './places-panel.js';

const COLUMNS: readonly TableColumn<MachineRow>[] = [
  {key: 'name', header: 'Name', lead: true, clip: row => row.label, cell: row => row.label},
  {
    key: 'status',
    header: 'Status',
    cell: row => <HostStatusChip status={row.status} even />,
  },
];

/** The row action that removes a machine; this machine has none. */
const removeColumn = (remove: (key: string) => void): TableColumn<MachineRow> => ({
  key: 'remove',
  header: 'Remove',
  control: true,
  cell: row =>
    row.key === 'local' ? null : (
      <Button
        variant="quiet"
        size="sm"
        icon="cross"
        label={`Remove ${row.label}`}
        hint="Remove"
        press={() => remove(row.key)}
      />
    ),
});

/**
 * Machines: every machine the app is connected to, one row each with its one status, on the page
 * frame the runs use. With `controls` the page adds and removes machines; without them it lists.
 */
export function MachinesScreen(props: {
  readonly rows: readonly MachineRow[];
  /** The Add machine and remove controls; a window without them lists the machines only. */
  readonly controls?: MachineControls;
  /** Each machine's live CPU, memory and GPU readings; without them the page lists the machines only. */
  readonly stats?: readonly MachineStatsHost[];
  /** Each machine's eval places and the refresh of one; without them the page lists the machines only. */
  readonly places?: {
    readonly hosts: readonly PlacesHost[];
    readonly refresh: (key: string) => void;
  };
}): JSX.Element {
  const {controls, stats, places} = props;
  const table = (
    <Table
      label="Machines"
      columns={controls === undefined ? COLUMNS : [...COLUMNS, removeColumn(controls.remove)]}
      rows={props.rows}
      rowKey={row => row.key}
      middle
      {...(controls?.selected == null ? {} : {selected: controls.selected})}
      {...(controls === undefined ? {} : {select: controls.select})}
    />
  );
  return (
    <Page width="wide" state="ready" title="Machines">
      <Stack gap="space-4">
        {controls?.bar}
        {table}
        {stats === undefined ? null : <MachineStatsPanel hosts={stats} />}
        {places === undefined ? null : (
          <PlacesPanel hosts={places.hosts} refresh={places.refresh} />
        )}
      </Stack>
      {controls?.toasts}
    </Page>
  );
}
